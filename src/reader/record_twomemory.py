# Copyright 2021 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Run BERT on ReCoRD."""

import math
import json
import random
import collections
import os
import pickle
import logging
import six
import src.reader.tokenization as tokenization
from src.reader.batching_twomemory import prepare_batch_data

from src.reader.record_official_evaluate import evaluate, f1_score

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)


class ReCoRDExample:
    """A single training/test example for simple sequence classification.

     For examples without an answer, the start and end position are -1.
  """

    def __init__(self,
                 qas_id,
                 question_text,
                 doc_tokens,
                 orig_answer_text=None,
                 start_position=None,
                 end_position=None,
                 is_impossible=False):
        self.qas_id = qas_id
        self.question_text = question_text
        self.doc_tokens = doc_tokens
        self.orig_answer_text = orig_answer_text
        self.start_position = start_position
        self.end_position = end_position
        self.is_impossible = is_impossible

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        s = ""
        s += "qas_id: %s" % (tokenization.printable_text(self.qas_id))
        s += ", question_text: %s" % (
            tokenization.printable_text(self.question_text))
        s += ", doc_tokens: [%s]" % (" ".join(self.doc_tokens))
        if self.start_position:
            s += ", start_position: %d" % (self.start_position)
        if self.start_position:
            s += ", end_position: %d" % (self.end_position)
        if self.start_position:
            s += ", is_impossible: %r" % (self.is_impossible)
        return s


class InputFeatures:
    """A single set of features of data."""

    def __init__(self,
                 unique_id,
                 example_index,
                 doc_span_index,
                 tokens,
                 token_to_orig_map,
                 token_is_max_context,
                 input_ids,
                 input_mask,
                 segment_ids,
                 wn_concept_ids,
                 nell_concept_ids,
                 start_position=None,
                 end_position=None,
                 is_impossible=None):
        self.unique_id = unique_id
        self.example_index = example_index
        self.doc_span_index = doc_span_index
        self.tokens = tokens
        self.token_to_orig_map = token_to_orig_map
        self.token_is_max_context = token_is_max_context
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.start_position = start_position
        self.end_position = end_position
        self.is_impossible = is_impossible
        self.wn_concept_ids = wn_concept_ids
        self.nell_concept_ids = nell_concept_ids


def read_record_examples(input_file, is_training, version_2_with_negative=False):
    """Read a ReCoRD json file into a list of ReCoRDExample."""
    with open(input_file, "r") as reader:
        input_data = json.load(reader)["data"]

    def is_whitespace(c):
        if c == " " or c == "\t" or c == "\r" or c == "\n" or ord(c) == 0x202F:
            return True
        return False

    examples = []
    for entry in input_data:
        paragraph_text = entry["passage"]["text"].replace('\xa0', ' ')
        doc_tokens = []
        char_to_word_offset = []
        prev_is_whitespace = True
        for c in paragraph_text:
            if is_whitespace(c):
                prev_is_whitespace = True
            else:
                if prev_is_whitespace:
                    doc_tokens.append(c)
                else:
                    doc_tokens[-1] += c
                prev_is_whitespace = False
            char_to_word_offset.append(len(doc_tokens) - 1)

        for qa in entry["qas"]:
            qas_id = qa["id"]
            question_text = qa["query"].replace('\xa0', ' ')
            start_position = None
            end_position = None
            orig_answer_text = None
            is_impossible = False
            if is_training:

                if version_2_with_negative:
                    is_impossible = qa["is_impossible"]
                # if (len(qa["answers"]) != 1) and (not is_impossible):
                #     raise ValueError(
                #         "For training, each question should have exactly 1 answer."
                #     )
                if not is_impossible:
                    answer = qa["answers"][0]
                    orig_answer_text = answer["text"]
                    answer_offset = answer["start"]
                    answer_length = len(orig_answer_text)
                    start_position = char_to_word_offset[answer_offset]
                    end_position = char_to_word_offset[answer_offset +
                                                       answer_length - 1]
                    # Only add answers where the text can be exactly recovered from the
                    # document. If this CAN'T happen it's likely due to weird Unicode
                    # stuff so we will just skip the example.
                    #
                    # Note that this means for training mode, every example is NOT
                    # guaranteed to be preserved.
                    actual_text = " ".join(doc_tokens[start_position:(end_position + 1)])
                    cleaned_answer_text = " ".join(
                        tokenization.whitespace_tokenize(orig_answer_text))
                    if actual_text.find(cleaned_answer_text) == -1:
                        logger.info("Could not find answer: '%s' vs. '%s'",
                                    actual_text, cleaned_answer_text)
                        continue
                else:
                    start_position = -1
                    end_position = -1
                    orig_answer_text = ""

            example = ReCoRDExample(
                qas_id=qas_id,
                question_text=question_text,
                doc_tokens=doc_tokens,
                orig_answer_text=orig_answer_text,
                start_position=start_position,
                end_position=end_position,
                is_impossible=is_impossible)
            examples.append(example)

    return examples


class Examples_To_Features_Converter:
    """Examples to features converter"""
    def __init__(self, **concept_settings):
        self.concept_settings = concept_settings

        # load necessary data files for mapping to related concepts
        # 1. mapping from subword-level tokenization to word-level tokenization
        tokenization_filepath = self.concept_settings['tokenization_path']
        assert os.path.exists(tokenization_filepath)
        self.all_tokenization_info = {}
        for item in pickle.load(open(tokenization_filepath, 'rb')):
            self.all_tokenization_info[item['id']] = item

        # 2. mapping from concept name to concept id
        self.wn_concept2id = self.concept_settings['wn_concept2id']
        self.nell_concept2id = self.concept_settings['nell_concept2id']

        # 3. retrieved related wordnet concepts (if use_wordnet)
        if concept_settings['use_wordnet']:
            retrieved_synset_filepath = self.concept_settings['retrieved_synset_path']
            assert os.path.exists(retrieved_synset_filepath)
            self.synsets_info = pickle.load(open(retrieved_synset_filepath, 'rb'))  # token to sysnet names
            self.max_wn_concept_length = max([len(synsets) for synsets in self.synsets_info.values()])

        # 4. retrieved related nell concepts (if use_nell)
        if concept_settings['use_nell']:
            retrieved_nell_concept_filepath = self.concept_settings['retrieved_nell_concept_path']
            assert os.path.exists(retrieved_nell_concept_filepath)
            self.nell_retrieve_info = {}
            for item in pickle.load(open(retrieved_nell_concept_filepath, 'rb')):
                self.nell_retrieve_info[item['id']] = item
            self.max_nell_concept_length = max([max([len(entity_info['retrieved_concepts']) for entity_info in
                                                     item['query_entities'] + item['document_entities']])
                                                for qid, item in self.nell_retrieve_info.items() if
                                                item['query_entities'] + item['document_entities']])

    # return list of concept ids given input subword list
    def _lookup_wordnet_concept_ids(self, sub_tokens, sub_to_ori_index, tokens, tolower, tokenizer):
        """lookup wordnet concept ids"""
        concept_ids = []
        for index in range(len(sub_tokens)):
            original_token = tokens[sub_to_ori_index[index]]
            # if tokens are in upper case, we must lower it for retrieving
            retrieve_token = tokenizer.basic_tokenizer.run_strip_accents(
                original_token.lower()) if tolower else original_token
            if retrieve_token in self.synsets_info:
                concept_ids.append(
                    [self.wn_concept2id[synset_name] for synset_name in self.synsets_info[retrieve_token]])
            else:
                concept_ids.append([])
        return concept_ids

    def _lookup_nell_concept_ids(self, sub_tokens, sub_to_ori_index, tokens, nell_info):
        original_concept_ids = [[] for _ in range(len(tokens))]
        for entity_info in nell_info:
            for pos in range(entity_info['token_start'], entity_info['token_end'] + 1):
                original_concept_ids[pos] += [self.nell_concept2id[category_name] for category_name in
                                              entity_info['retrieved_concepts']]
        for pos, original_concept_id in enumerate(original_concept_ids):
            original_concept_ids[pos] = list(set(original_concept_id))
        concept_ids = [original_concept_ids[sub_to_ori_index[index]] for index in range(len(sub_tokens))]
        return concept_ids

    def __call__(self,
                 examples,
                 tokenizer,
                 max_seq_length,
                 doc_stride,
                 max_query_length,
                 is_training):
        """Loads a data file into a list of `InputBatch`s."""

        unique_id = 1000000000

        for (example_index, example) in enumerate(examples):
            tokenization_info = self.all_tokenization_info[example.qas_id]
            query_tokens = tokenizer.tokenize(example.question_text)
            # check online subword tokenization result is the same as offline result
            assert query_tokens == tokenization_info['query_subtokens']
            if self.concept_settings['use_wordnet']:
                query_wn_concepts \
                    = self._lookup_wordnet_concept_ids(query_tokens,
                                                       tokenization_info['query_sub_to_ori_index'],
                                                       tokenization_info['query_tokens'],
                                                       tolower=not tokenizer.basic_tokenizer.do_lower_case,
                                                       tokenizer=tokenizer)
                # if tolower is True, tokenizer must be given

            if self.concept_settings['use_nell']:
                query_nell_concepts = self._lookup_nell_concept_ids(query_tokens,
                                                                    tokenization_info['query_sub_to_ori_index'],
                                                                    tokenization_info['query_tokens'],
                                                                    self.nell_retrieve_info[example.qas_id][
                                                                        'query_entities'])

            if len(query_tokens) > max_query_length:
                query_tokens = query_tokens[0:max_query_length]
                query_wn_concepts = query_wn_concepts[0:max_query_length]
                query_nell_concepts = query_nell_concepts[0:max_query_length]

            tok_to_orig_index = []
            orig_to_tok_index = []
            all_doc_tokens = []
            for (i, token) in enumerate(example.doc_tokens):
                orig_to_tok_index.append(len(all_doc_tokens))
                sub_tokens = tokenizer.tokenize(token)
                for sub_token in sub_tokens:
                    tok_to_orig_index.append(i)
                    all_doc_tokens.append(sub_token)
            assert all_doc_tokens == tokenization_info['document_subtokens']
            if self.concept_settings['use_wordnet']:
                doc_wn_concepts \
                    = self._lookup_wordnet_concept_ids(all_doc_tokens,
                                                       tokenization_info['document_sub_to_ori_index'],
                                                       tokenization_info['document_tokens'],
                                                       tolower=not tokenizer.basic_tokenizer.do_lower_case,
                                                       tokenizer=tokenizer)
                # if tolower is True, tokenizer must be given

            if self.concept_settings['use_nell']:
                doc_nell_concepts = self._lookup_nell_concept_ids(all_doc_tokens,
                                                                  tokenization_info['document_sub_to_ori_index'],
                                                                  tokenization_info['document_tokens'],
                                                                  self.nell_retrieve_info[example.qas_id][
                                                                      'document_entities'])

            tok_start_position = None
            tok_end_position = None
            if is_training and example.is_impossible:
                tok_start_position = -1
                tok_end_position = -1
            if is_training and not example.is_impossible:
                tok_start_position = orig_to_tok_index[example.start_position]
                if example.end_position < len(example.doc_tokens) - 1:
                    tok_end_position = orig_to_tok_index[example.end_position +
                                                         1] - 1
                else:
                    tok_end_position = len(all_doc_tokens) - 1
                (tok_start_position, tok_end_position) = _improve_answer_span(
                    all_doc_tokens, tok_start_position, tok_end_position, tokenizer,
                    example.orig_answer_text)

            # The -3 accounts for [CLS], [SEP] and [SEP]
            max_tokens_for_doc = max_seq_length - len(query_tokens) - 3

            # We can have documents that are longer than the maximum sequence length.
            # To deal with this we do a sliding window approach, where we take chunks
            # of the up to our max length with a stride of `doc_stride`.
            _DocSpan = collections.namedtuple(  # pylint: disable=invalid-name
                "DocSpan", ["start", "length"])
            doc_spans = []
            start_offset = 0
            while start_offset < len(all_doc_tokens):
                length = len(all_doc_tokens) - start_offset
                if length > max_tokens_for_doc:
                    length = max_tokens_for_doc
                doc_spans.append(_DocSpan(start=start_offset, length=length))
                if start_offset + length == len(all_doc_tokens):
                    break
                start_offset += min(length, doc_stride)

            for (doc_span_index, doc_span) in enumerate(doc_spans):
                tokens = []
                token_to_orig_map = {}
                token_is_max_context = {}
                segment_ids = []
                wn_concept_ids = []
                nell_concept_ids = []

                tokens.append("[CLS]")
                segment_ids.append(0)
                wn_concept_ids.append([])
                nell_concept_ids.append([])
                for token, query_wn_concept, query_nell_concept in zip(query_tokens, query_wn_concepts,
                                                                       query_nell_concepts):
                    tokens.append(token)
                    segment_ids.append(0)
                    wn_concept_ids.append(query_wn_concept)
                    nell_concept_ids.append(query_nell_concept)
                tokens.append("[SEP]")
                segment_ids.append(0)
                wn_concept_ids.append([])
                nell_concept_ids.append([])

                for i in range(doc_span.length):
                    split_token_index = doc_span.start + i
                    token_to_orig_map[len(tokens)] = tok_to_orig_index[split_token_index]

                    is_max_context = _check_is_max_context(doc_spans, doc_span_index,
                                                           split_token_index)
                    token_is_max_context[len(tokens)] = is_max_context
                    tokens.append(all_doc_tokens[split_token_index])
                    segment_ids.append(1)
                    wn_concept_ids.append(doc_wn_concepts[split_token_index])
                    nell_concept_ids.append(doc_nell_concepts[split_token_index])
                tokens.append("[SEP]")
                segment_ids.append(1)
                wn_concept_ids.append([])
                nell_concept_ids.append([])

                input_ids = tokenizer.convert_tokens_to_ids(tokens)

                # The mask has 1 for real tokens and 0 for padding tokens. Only real
                # tokens are attended to.
                input_mask = [1] * len(input_ids)

                # Zero-pad up to the sequence length.
                # while len(input_ids) < max_seq_length:
                #  input_ids.append(0)
                #  input_mask.append(0)
                #  segment_ids.append(0)

                # assert len(input_ids) == max_seq_length
                # assert len(input_mask) == max_seq_length
                # assert len(segment_ids) == max_seq_length

                for concept_ids, max_concept_length in zip((wn_concept_ids, nell_concept_ids),
                                                           (self.max_wn_concept_length, self.max_nell_concept_length)):
                    for cindex, concept_id in enumerate(concept_ids):
                        concept_ids[cindex] = concept_id + [0] * (max_concept_length - len(concept_id))
                        concept_ids[cindex] = concept_ids[cindex][:max_concept_length]
                    assert all([len(id_list) == max_concept_length for id_list in concept_ids])

                start_position = None
                end_position = None
                if is_training and not example.is_impossible:
                    # For training, if our document chunk does not contain an annotation
                    # we throw it out, since there is nothing to predict.
                    doc_start = doc_span.start
                    doc_end = doc_span.start + doc_span.length - 1
                    # out_of_span = False
                    if not (tok_start_position >= doc_start and
                            tok_end_position <= doc_end):
                        continue

                    doc_offset = len(query_tokens) + 2
                    start_position = tok_start_position - doc_start + doc_offset
                    end_position = tok_end_position - doc_start + doc_offset

                if is_training and example.is_impossible:
                    start_position = 0
                    end_position = 0

                if example_index < 3:
                    logger.info("*** Example ***")
                    logger.info("unique_id: %s", unique_id)
                    logger.info("example_index: %s", example_index)
                    logger.info("doc_span_index: %s", doc_span_index)
                    logger.info("tokens: %s", " ".join(
                        [tokenization.printable_text(x) for x in tokens]))
                    logger.info("token_to_orig_map: %s", " ".join([
                        "%d:%d" % (x, y)
                        for (x, y) in six.iteritems(token_to_orig_map)
                    ]))
                    logger.info("token_is_max_context: %s", " ".join([
                        "%d:%s" % (x, y)
                        for (x, y) in six.iteritems(token_is_max_context)
                    ]))
                    logger.info("input_ids: %s", " ".join([str(x) for x in input_ids]))
                    logger.info("input_mask: %s", " ".join([str(x) for x in input_mask]))
                    logger.info("segment_ids: %s",
                                " ".join([str(x) for x in segment_ids]))
                    logger.info("wordnet_concept_ids: %s", " ".join(
                        ["{}:{}".format(tidx, list(filter(lambda index: index != 0, x))) for tidx, x in
                         enumerate(wn_concept_ids)]))
                    logger.info("nell_concept_ids: %s", " ".join(
                        ["{}:{}".format(tidx, list(filter(lambda index: index != 0, x))) for tidx, x in
                         enumerate(nell_concept_ids)]))
                    if is_training and example.is_impossible:
                        logger.info("impossible example")
                    if is_training and not example.is_impossible:
                        answer_text = " ".join(tokens[start_position:(end_position +
                                                                      1)])
                        logger.info("start_position: %d", start_position)
                        logger.info("end_position: %d", end_position)
                        logger.info("answer: %s",
                                    (tokenization.printable_text(answer_text)))

                feature = InputFeatures(
                    unique_id=unique_id,
                    example_index=example_index,
                    doc_span_index=doc_span_index,
                    tokens=tokens,
                    token_to_orig_map=token_to_orig_map,
                    token_is_max_context=token_is_max_context,
                    input_ids=input_ids,
                    input_mask=input_mask,
                    segment_ids=segment_ids,
                    wn_concept_ids=wn_concept_ids,
                    nell_concept_ids=nell_concept_ids,
                    start_position=start_position,
                    end_position=end_position,
                    is_impossible=example.is_impossible)

                unique_id += 1

                yield feature


def _improve_answer_span(doc_tokens, input_start, input_end, tokenizer,
                         orig_answer_text):
    """Returns tokenized answer spans that better match the annotated answer."""

    # The ReCoRD annotations are character based. We first project them to
    # whitespace-tokenized words. But then after WordPiece tokenization, we can
    # often find a "better match". For example:
    #
    #   Question: What year was John Smith born?
    #   Context: The leader was John Smith (1895-1943).
    #   Answer: 1895
    #
    # The original whitespace-tokenized answer will be "(1895-1943).". However
    # after tokenization, our tokens will be "( 1895 - 1943 ) .". So we can match
    # the exact answer, 1895.
    #
    # However, this is not always possible. Consider the following:
    #
    #   Question: What country is the top exporter of electornics?
    #   Context: The Japanese electronics industry is the lagest in the world.
    #   Answer: Japan
    #
    # In this case, the annotator chose "Japan" as a character sub-span of
    # the word "Japanese". Since our WordPiece tokenizer does not split
    # "Japanese", we just use "Japanese" as the annotation. This is fairly rare
    # in ReCoRD, but does happen.
    tok_answer_text = " ".join(tokenizer.tokenize(orig_answer_text))

    for new_start in range(input_start, input_end + 1):
        for new_end in range(input_end, new_start - 1, -1):
            text_span = " ".join(doc_tokens[new_start:(new_end + 1)])
            if text_span == tok_answer_text:
                return new_start, new_end

    return input_start, input_end


def _check_is_max_context(doc_spans, cur_span_index, position):
    """Check if this is the 'max context' doc span for the token."""

    # Because of the sliding window approach taken to scoring documents, a single
    # token can appear in multiple documents. E.g.
    #  Doc: the man went to the store and bought a gallon of milk
    #  Span A: the man went to the
    #  Span B: to the store and bought
    #  Span C: and bought a gallon of
    #  ...
    #
    # Now the word 'bought' will have two scores from spans B and C. We only
    # want to consider the score with "maximum context", which we define as
    # the *minimum* of its left and right context (the *sum* of left and
    # right context will always be the same, of course).
    #
    # In the example the maximum context for 'bought' would be span C since
    # it has 1 left context and 3 right context, while span B has 4 left context
    # and 0 right context.
    best_score = None
    best_span_index = None
    for (span_index, doc_span) in enumerate(doc_spans):
        end = doc_span.start + doc_span.length - 1
        if position < doc_span.start:
            continue
        if position > end:
            continue
        num_left_context = position - doc_span.start
        num_right_context = end - position
        score = min(num_left_context,
                    num_right_context) + 0.01 * doc_span.length
        if best_score is None or score > best_score:
            best_score = score
            best_span_index = span_index

    return cur_span_index == best_span_index


class DataProcessor:
    """process data"""
    def __init__(self, vocab_path, do_lower_case, max_seq_length, in_tokens,
                 doc_stride, max_query_length):
        self._tokenizer = tokenization.FullTokenizer(
            vocab_file=vocab_path, do_lower_case=do_lower_case)
        self._max_seq_length = max_seq_length
        self._doc_stride = doc_stride
        self._max_query_length = max_query_length
        self._in_tokens = in_tokens

        self.vocab = self._tokenizer.vocab
        self.vocab_size = len(self.vocab)
        self.pad_id = self.vocab["[PAD]"]
        self.cls_id = self.vocab["[CLS]"]
        self.sep_id = self.vocab["[SEP]"]
        self.mask_id = self.vocab["[MASK]"]

        self.current_train_example = -1
        self.num_train_examples = -1
        self.current_train_epoch = -1

        self.train_examples = None
        self.predict_examples = None
        self.num_examples = {'train': -1, 'predict': -1}

        self.train_wn_max_concept_length = None
        self.predict_wn_max_concept_length = None
        self.train_nell_max_concept_length = None
        self.predict_nell_max_concept_length = None

    def get_train_progress(self):
        """Gets progress for training phase."""
        return self.current_train_example, self.current_train_epoch

    def get_examples(self,
                     data_path,
                     is_training,
                     version_2_with_negative=False):
        examples = read_record_examples(
            input_file=data_path,
            is_training=is_training,
            version_2_with_negative=version_2_with_negative)
        return examples

    def get_num_examples(self, phase):
        if phase not in ['train', 'predict']:
            raise ValueError(
                "Unknown phase, which should be in ['train', 'predict'].")
        return self.num_examples[phase]

    def get_features(self, examples, is_training, **concept_settings):
        convert_examples_to_features = Examples_To_Features_Converter(**concept_settings)
        features = convert_examples_to_features(
            examples=examples,
            tokenizer=self._tokenizer,
            max_seq_length=self._max_seq_length,
            doc_stride=self._doc_stride,
            max_query_length=self._max_query_length,
            is_training=is_training)
        return features

    def data_generator(self,
                       data_path,
                       batch_size,
                       phase='train',
                       shuffle=False,
                       dev_count=1,
                       version_2_with_negative=False,
                       epoch=1,
                       **concept_settings):
        """generate data"""
        if phase == 'train':
            self.train_examples = self.get_examples(
                data_path,
                is_training=True,
                version_2_with_negative=version_2_with_negative)
            examples = self.train_examples
            self.num_examples['train'] = len(self.train_examples)
        elif phase == 'predict':
            self.predict_examples = self.get_examples(
                data_path,
                is_training=False,
                version_2_with_negative=version_2_with_negative)
            examples = self.predict_examples
            self.num_examples['predict'] = len(self.predict_examples)
        else:
            raise ValueError(
                "Unknown phase, which should be in ['train', 'predict'].")

        def batch_reader(features, batch_size, in_tokens):
            batch, total_token_num, max_len = [], 0, 0
            for (index, feature) in enumerate(features):
                if phase == 'train':
                    self.current_train_example = index + 1
                seq_len = len(feature.input_ids)
                labels = [feature.unique_id] if feature.start_position is None else [feature.start_position,
                                                                                     feature.end_position]
                example = [feature.input_ids, feature.segment_ids, range(384), feature.wn_concept_ids,
                           feature.nell_concept_ids] + labels
                max_len = max(max_len, seq_len)

                # max_len = max(max_len, len(token_ids))
                if in_tokens:
                    to_append = (len(batch) + 1) * max_len <= batch_size
                else:
                    to_append = len(batch) < batch_size

                if to_append:
                    batch.append(example)
                    total_token_num += seq_len
                else:
                    yield batch, total_token_num
                    batch, total_token_num, max_len = [example], seq_len, seq_len
            if batch:
                yield batch, total_token_num

        if phase == 'train':
            self.train_wn_max_concept_length = Examples_To_Features_Converter(**concept_settings).max_wn_concept_length
            self.train_nell_max_concept_length = Examples_To_Features_Converter(
                **concept_settings).max_nell_concept_length
        else:
            self.predict_wn_max_concept_length = Examples_To_Features_Converter(
                **concept_settings).max_wn_concept_length
            self.predict_nell_max_concept_length = Examples_To_Features_Converter(
                **concept_settings).max_nell_concept_length

        def wrapper():
            for epoch_index in range(epoch):
                if shuffle:
                    random.shuffle(examples)
                if phase == 'train':
                    self.current_train_epoch = epoch_index
                    features = self.get_features(examples, is_training=True, **concept_settings)
                    max_wn_concept_length = self.train_wn_max_concept_length
                    max_nell_concept_length = self.train_nell_max_concept_length
                else:
                    features = self.get_features(examples, is_training=False, **concept_settings)
                    max_wn_concept_length = self.predict_wn_max_concept_length
                    max_nell_concept_length = self.predict_nell_max_concept_length

                all_dev_batches = []
                for batch_data, total_token_num in batch_reader(
                        features, batch_size, self._in_tokens):
                    batch_data = prepare_batch_data(
                        batch_data,
                        total_token_num,
                        voc_size=-1,
                        pad_id=self.pad_id,
                        cls_id=self.cls_id,
                        sep_id=self.sep_id,
                        mask_id=-1,
                        max_wn_concept_length=max_wn_concept_length,
                        max_nell_concept_length=max_nell_concept_length)
                    if len(all_dev_batches) < dev_count:
                        all_dev_batches.append(batch_data)

                    if len(all_dev_batches) == dev_count:
                        for batch in all_dev_batches:
                            yield batch
                        all_dev_batches = []

        return wrapper


def write_predictions(all_examples, all_features, all_results, n_best_size,
                      max_answer_length, do_lower_case, output_prediction_file,
                      output_nbest_file, output_null_log_odds_file,
                      version_2_with_negative, null_score_diff_threshold,
                      verbose, predict_file, evaluation_result_file):
    """Write final predictions to the json file and log-odds of null if needed."""
    logger.info("Writing predictions to: %s", output_prediction_file)
    logger.info("Writing nbest to: %s", output_nbest_file)
    logger.info("Writing evaluation result to: %s", evaluation_result_file)

    # load ground truth file for evaluation and post-edit
    with open(predict_file, "r", encoding='utf-8') as reader:
        predict_json = json.load(reader)["data"]
        all_candidates = {}
        for passage in predict_json:
            passage_text = passage['passage']['text']
            candidates = []
            for entity_info in passage['passage']['entities']:
                start_offset = entity_info['start']
                end_offset = entity_info['end']
                candidates.append(passage_text[start_offset: end_offset + 1])
            for qa in passage['qas']:
                all_candidates[qa['id']] = candidates

    example_index_to_features = collections.defaultdict(list)
    for feature in all_features:
        example_index_to_features[feature.example_index].append(feature)

    unique_id_to_result = {}
    for result in all_results:
        unique_id_to_result[result.unique_id] = result

    _PrelimPrediction = collections.namedtuple(  # pylint: disable=invalid-name
        "PrelimPrediction", [
            "feature_index", "start_index", "end_index", "start_logit",
            "end_logit"
        ])

    all_predictions = collections.OrderedDict()
    all_nbest_json = collections.OrderedDict()
    scores_diff_json = collections.OrderedDict()

    for (example_index, example) in enumerate(all_examples):
        features = example_index_to_features[example_index]

        prelim_predictions = []
        # keep track of the minimum score of null start+end of position 0
        score_null = 1000000  # large and positive
        min_null_feature_index = 0  # the paragraph slice with min mull score
        null_start_logit = 0  # the start logit at the slice with min null score
        null_end_logit = 0  # the end logit at the slice with min null score
        for (feature_index, feature) in enumerate(features):
            result = unique_id_to_result[feature.unique_id]
            start_indexes = _get_best_indexes(result.start_logits, n_best_size)
            end_indexes = _get_best_indexes(result.end_logits, n_best_size)
            # if we could have irrelevant answers, get the min score of irrelevant
            if version_2_with_negative:
                feature_null_score = result.start_logits[0] + result.end_logits[
                    0]
                if feature_null_score < score_null:
                    score_null = feature_null_score
                    min_null_feature_index = feature_index
                    null_start_logit = result.start_logits[0]
                    null_end_logit = result.end_logits[0]
            for start_index in start_indexes:
                for end_index in end_indexes:
                    # We could hypothetically create invalid predictions, e.g., predict
                    # that the start of the span is in the question. We throw out all
                    # invalid predictions.
                    if start_index >= len(feature.tokens):
                        continue
                    if end_index >= len(feature.tokens):
                        continue
                    if start_index not in feature.token_to_orig_map:
                        continue
                    if end_index not in feature.token_to_orig_map:
                        continue
                    if not feature.token_is_max_context.get(start_index, False):
                        continue
                    if end_index < start_index:
                        continue
                    length = end_index - start_index + 1
                    if length > max_answer_length:
                        continue
                    prelim_predictions.append(
                        _PrelimPrediction(
                            feature_index=feature_index,
                            start_index=start_index,
                            end_index=end_index,
                            start_logit=result.start_logits[start_index],
                            end_logit=result.end_logits[end_index]))

        if version_2_with_negative:
            prelim_predictions.append(
                _PrelimPrediction(
                    feature_index=min_null_feature_index,
                    start_index=0,
                    end_index=0,
                    start_logit=null_start_logit,
                    end_logit=null_end_logit))
        prelim_predictions = sorted(
            prelim_predictions,
            key=lambda x: (x.start_logit + x.end_logit),
            reverse=True)

        _NbestPrediction = collections.namedtuple(  # pylint: disable=invalid-name
            "NbestPrediction", ["text", "start_logit", "end_logit"])

        seen_predictions = {}
        nbest = []
        for pred in prelim_predictions:
            if len(nbest) >= n_best_size:
                break
            feature = features[pred.feature_index]
            if pred.start_index > 0:  # this is a non-null prediction
                tok_tokens = feature.tokens[pred.start_index:(pred.end_index + 1
                                                              )]
                orig_doc_start = feature.token_to_orig_map[pred.start_index]
                orig_doc_end = feature.token_to_orig_map[pred.end_index]
                orig_tokens = example.doc_tokens[orig_doc_start:(orig_doc_end +
                                                                 1)]
                tok_text = " ".join(tok_tokens)

                # De-tokenize WordPieces that have been split off.
                tok_text = tok_text.replace(" ##", "")
                tok_text = tok_text.replace("##", "")

                # Clean whitespace
                tok_text = tok_text.strip()
                tok_text = " ".join(tok_text.split())
                orig_text = " ".join(orig_tokens)

                final_text = get_final_text(tok_text, orig_text, do_lower_case,
                                            verbose)
                if final_text in seen_predictions:
                    continue

                seen_predictions[final_text] = True
            else:
                final_text = ""
                seen_predictions[final_text] = True

            nbest.append(
                _NbestPrediction(
                    text=final_text,
                    start_logit=pred.start_logit,
                    end_logit=pred.end_logit))

        # if we didn't include the empty option in the n-best, include it
        if version_2_with_negative:
            if "" not in seen_predictions:
                nbest.append(
                    _NbestPrediction(
                        text="",
                        start_logit=null_start_logit,
                        end_logit=null_end_logit))
        # In very rare edge cases we could have no valid predictions. So we
        # just create a nonce prediction in this case to avoid failure.
        if not nbest:
            nbest.append(
                _NbestPrediction(
                    text="empty", start_logit=0.0, end_logit=0.0))

        assert len(nbest) >= 1

        total_scores = []
        best_non_null_entry = None
        for entry in nbest:
            total_scores.append(entry.start_logit + entry.end_logit)
            if not best_non_null_entry:
                if entry.text:
                    best_non_null_entry = entry
        # debug
        if best_non_null_entry is None:
            logger.info("Emmm..., sth wrong")

        probs = _compute_softmax(total_scores)

        nbest_json = []
        for (i, entry) in enumerate(nbest):
            output = collections.OrderedDict()
            output["text"] = entry.text
            output["probability"] = probs[i]
            output["start_logit"] = entry.start_logit
            output["end_logit"] = entry.end_logit
            nbest_json.append(output)

        assert len(nbest_json) >= 1

        if not version_2_with_negative:
            # restrict the finally picked prediction to have overlap with at least one candidate
            picked_index = 0
            for pred_index in range(len(nbest_json)):
                if any([f1_score(nbest_json[pred_index]['text'], candidate) > 0. for candidate in
                        all_candidates[example.qas_id]]):
                    picked_index = pred_index
                    break
            all_predictions[example.qas_id] = nbest_json[picked_index]["text"]
        else:
            # predict "" iff the null score - the score of best non-null > threshold
            score_diff = score_null - best_non_null_entry.start_logit - (
                best_non_null_entry.end_logit)
            scores_diff_json[example.qas_id] = score_diff
            if score_diff > null_score_diff_threshold:
                all_predictions[example.qas_id] = ""
            else:
                all_predictions[example.qas_id] = best_non_null_entry.text

        all_nbest_json[example.qas_id] = nbest_json

    with open(output_prediction_file, "w") as writer:
        writer.write(json.dumps(all_predictions, indent=4) + "\n")

    with open(output_nbest_file, "w") as writer:
        writer.write(json.dumps(all_nbest_json, indent=4) + "\n")

    if version_2_with_negative:
        with open(output_null_log_odds_file, "w") as writer:
            writer.write(json.dumps(scores_diff_json, indent=4) + "\n")

    eval_result, _ = evaluate(predict_json, all_predictions)

    with open(evaluation_result_file, "w") as writer:
        writer.write(json.dumps(eval_result, indent=4) + "\n")

    return eval_result


def get_final_text(pred_text, orig_text, do_lower_case, verbose):
    """Project the tokenized prediction back to the original text."""

    # When we created the data, we kept track of the alignment between original
    # (whitespace tokenized) tokens and our WordPiece tokenized tokens. So
    # now `orig_text` contains the span of our original text corresponding to the
    # span that we predicted.
    #
    # However, `orig_text` may contain extra characters that we don't want in
    # our prediction.
    #
    # For example, let's say:
    #   pred_text = steve smith
    #   orig_text = Steve Smith's
    #
    # We don't want to return `orig_text` because it contains the extra "'s".
    #
    # We don't want to return `pred_text` because it's already been normalized
    # (the ReCoRD eval script also does punctuation stripping/lower casing but
    # our tokenizer does additional normalization like stripping accent
    # characters).
    #
    # What we really want to return is "Steve Smith".
    #
    # Therefore, we have to apply a semi-complicated alignment heruistic between
    # `pred_text` and `orig_text` to get a character-to-charcter alignment. This
    # can fail in certain cases in which case we just return `orig_text`.

    def _strip_spaces(text):
        ns_chars = []
        ns_to_s_map = collections.OrderedDict()
        for (i, c) in enumerate(text):
            if c == " ":
                continue
            ns_to_s_map[len(ns_chars)] = i
            ns_chars.append(c)
        ns_text = "".join(ns_chars)
        return ns_text, ns_to_s_map

    # We first tokenize `orig_text`, strip whitespace from the result
    # and `pred_text`, and check if they are the same length. If they are
    # NOT the same length, the heuristic has failed. If they are the same
    # length, we assume the characters are one-to-one aligned.
    tokenizer = tokenization.BasicTokenizer(do_lower_case=do_lower_case)

    tok_text = " ".join(tokenizer.tokenize(orig_text))

    start_position = tok_text.find(pred_text)
    if start_position == -1:
        if verbose:
            logger.info("Unable to find text: '%s' in '%s'", pred_text, orig_text)
        return orig_text
    end_position = start_position + len(pred_text) - 1

    (orig_ns_text, orig_ns_to_s_map) = _strip_spaces(orig_text)
    (tok_ns_text, tok_ns_to_s_map) = _strip_spaces(tok_text)

    if len(orig_ns_text) != len(tok_ns_text):
        if verbose:
            logger.info("Length not equal after stripping spaces: '%s' vs '%s'",
                        orig_ns_text, tok_ns_text)
        return orig_text

    # We then project the characters in `pred_text` back to `orig_text` using
    # the character-to-character alignment.
    tok_s_to_ns_map = {}
    for (i, tok_index) in six.iteritems(tok_ns_to_s_map):
        tok_s_to_ns_map[tok_index] = i

    orig_start_position = None
    if start_position in tok_s_to_ns_map:
        ns_start_position = tok_s_to_ns_map[start_position]
        if ns_start_position in orig_ns_to_s_map:
            orig_start_position = orig_ns_to_s_map[ns_start_position]

    if orig_start_position is None:
        if verbose:
            logger.info("Couldn't map start position")
        return orig_text

    orig_end_position = None
    if end_position in tok_s_to_ns_map:
        ns_end_position = tok_s_to_ns_map[end_position]
        if ns_end_position in orig_ns_to_s_map:
            orig_end_position = orig_ns_to_s_map[ns_end_position]

    if orig_end_position is None:
        if verbose:
            logger.info("Couldn't map end position")
        return orig_text

    output_text = orig_text[orig_start_position:(orig_end_position + 1)]
    return output_text


def _get_best_indexes(logits, n_best_size):
    """Get the n-best logits from a list."""
    index_and_score = sorted(
        enumerate(logits), key=lambda x: x[1], reverse=True)

    best_indexes = []
    for i, score in enumerate(index_and_score):
        if i >= n_best_size:
            break
        best_indexes.append(score[0])
    return best_indexes


def _compute_softmax(scores):
    """Compute softmax probability over raw logits."""
    if not scores:
        return []

    max_score = None
    for score in scores:
        if max_score is None or score > max_score:
            max_score = score

    exp_scores = []
    total_sum = 0.0
    for score in scores:
        x = math.exp(score - max_score)
        exp_scores.append(x)
        total_sum += x

    probs = []
    for score in exp_scores:
        probs.append(score / total_sum)
    return probs
