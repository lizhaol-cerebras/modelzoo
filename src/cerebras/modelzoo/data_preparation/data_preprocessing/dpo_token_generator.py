# Copyright 2022 Cerebras Systems.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from typing import Any, Dict, List, Tuple

import ftfy
import numpy as np

from cerebras.modelzoo.data_preparation.data_preprocessing.utils import (
    wikitext_detokenizer,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

from typing import Dict, List

import numpy as np


class DPOTokenGenerator:
    def __init__(
        self, params: Dict[str, Any], tokenizer, eos_id: int, pad_id: int
    ):
        """
        Initialize the DPTokenGenerator class with dataset parameters, tokenizer, and token IDs.

        Args:
            params (Dict[str, Any]): A dictionary containing parameters for dataset processing and configurations.
                                      It should include 'dataset' and 'processing' keys among others.
            tokenizer: An instance of a tokenizer, likely from the Hugging Face transformers library.
            eos_id (int): The token ID used to signify the end of a sequence.
            pad_id (int): The token ID used for padding sequences to a uniform length.

        The function initializes the DPTokenGenerator with various settings for text processing, including
        flags for text normalization, detokenization options, data types for input IDs and masks,
        special token configurations, and sequence length constraints.
        """
        dataset_params = params["dataset"]
        processing_params = params["processing"]
        self.tokenizer = tokenizer

        # Extracting and setting parameters from the dataset_params dictionary
        self.use_ftfy = dataset_params.pop("use_ftfy", False)
        self.ftfy_normalizer = dataset_params.pop("ftfy_normalizer", "NFC")
        self.wikitext_detokenize = dataset_params.pop(
            "wikitext_detokenize", False
        )
        self.input_ids_dtype = dataset_params.pop("input_ids_dtype", "int32")
        self.input_mask_dtype = dataset_params.pop("input_mask_dtype", "int32")
        self.sep_token = dataset_params.pop("sep_token", None)
        self.sep_id = None
        if self.sep_token:
            self.sep_id = self.get_token_id(
                self.sep_token
            )  # Assuming this method exists or is implemented elsewhere
        self.inverted_mask = dataset_params.pop("inverted_mask", False)
        self.min_sequence_len = dataset_params.pop("min_sequence_len", 10)

        # Extracting and setting parameters from the processing_params dictionary
        self.max_seq_length = processing_params.pop("max_seq_length", 2048)
        self.max_prompt_length = processing_params.pop("max_prompt_length", 512)
        self.eos_id = eos_id
        self.pad_id = pad_id

        if (
            tokenizer.name_or_path == "gpt2-tokenizer"
            or tokenizer.name_or_path == "neox-tokenizer"
        ):
            self.can_apply_chat_template = False
        else:
            self.can_apply_chat_template = hasattr(
                self.tokenizer, 'add_special_tokens'
            ) and hasattr(self.tokenizer, 'apply_chat_template')
            # Additional tokenizer configuration (this assumes such a method exists on the tokenizer)
            if self.can_apply_chat_template:
                self.tokenizer.add_special_tokens(
                    {
                        "sep_token": "",
                        "cls_token": "",
                        "mask_token": "",
                        "pad_token": "",
                    }
                )

        # Setting roles and delimiter based on dataset_params
        self.user_role = dataset_params.pop("user_role", "user")
        self.assistant_role = dataset_params.pop("assistant_role", "assistant")
        self.response_delimiter = dataset_params.pop(
            "response_delimiter", "<response>"
        )

        # Handling the case where bos_token_id might not be set
        self.has_bos_token_id = (
            hasattr(self.tokenizer, 'bos_token_id')
            and self.tokenizer.bos_token_id is not None
        )

        self.add_bos_token = (
            hasattr(self.tokenizer, 'add_bos_token')
            and self.tokenizer.add_bos_token is True
        )

        if self.has_bos_token_id:
            self.bos_token_id = self.tokenizer.bos_token_id
        else:
            # Log a warning if the tokenizer's beginning-of-sequence token ID is not set
            logger.warning(
                f"tokenizer bos_token_id is None or does not exist. Setting it to eos_token_id."
            )
            self.bos_token_id = self.eos_id

        self.chat_template = dataset_params.pop("chat_template", None)
        self.sample_features = [
            "chosen_input_ids",
            "chosen_attention_mask",
            "chosen_labels",
            "rejected_input_ids",
            "rejected_attention_mask",
            "rejected_labels",
        ]

    def tokenize_text(self, text: str) -> Dict[str, List[int]]:
        """
        Tokenizes text with the tokenizer, supporting both callable tokenizers
        and those requiring an `encode` method.

        Args:
            text: Text to tokenize.

        Returns:
            Dictionary with 'input_ids', 'attention_mask', and 'labels'.
        """
        if callable(self.tokenizer):
            # Use callable tokenizer directly, assuming it returns a dict
            # with 'input_ids' and 'attention_mask'.
            return self.tokenizer(text)

        # Otherwise, use `encode` method to get token IDs.
        token_ids = self.tokenizer.encode(text)

        # Prepare input_ids.
        input_ids = token_ids

        # Labels are token IDs.
        labels = token_ids

        # Attention_mask of 1s for each token in input_ids.
        attention_mask = [1] * len(input_ids)

        # Package input_ids, attention_mask, and labels.
        features = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "labels": labels,
        }

        return features

    def build_tokenized_answer(
        self, prompt: str, prompt_response: str
    ) -> Dict[str, List[int]]:
        """
        Tokenizes the prompt and its response using a specific strategy to handle tokenizers
        where encoding a concatenated string does not simply equal the concatenation of
        encoded strings. Specifically handles cases for tokenizers like Llama's, ensuring
        that `enc(a + b) = enc(a) + enc(a + b)[len(enc(a)):]` holds.

        Args:
            tokenizer (PreTrainedTokenizer): The tokenizer to use for encoding.
            prompt (str): The prompt text to be encoded.
            prompt_response (str): The prompt response text to be encoded.

        Returns:
            Dict[str, List[int]]: A dictionary containing tokenized IDs and attention masks
            for both the prompt and the combined prompt and response.

        Raises:
            ValueError: If the lengths of generated token IDs do not match expectations.

        Reference:
            Discussion on tokenization strategy: https://github.com/EleutherAI/lm-evaluation-harness/pull/531#issuecomment-1595586257
        """

        # Tokenize the prompt response without adding special tokens
        full_tokenized = self.tokenize_text(prompt_response)

        # Tokenize the prompt to get its input IDs
        prompt_input_ids = self.tokenize_text(prompt)["input_ids"]

        # Extract answer's input IDs and attention mask based on the length of the prompt's input IDs
        answer_input_ids = full_tokenized["input_ids"][len(prompt_input_ids) :]
        answer_attention_mask = full_tokenized["attention_mask"][
            len(prompt_input_ids) :
        ]

        # Concatenate tokens to form `enc(a) + enc(a + b)[len(enc(a)):]`
        full_concat_input_ids = np.concatenate(
            [prompt_input_ids, answer_input_ids]
        )

        # Prepare input tokens for token by token comparison
        full_input_ids = np.array(full_tokenized["input_ids"])

        # Check if lengths match, raise an error if they do not
        if len(full_input_ids) != len(full_concat_input_ids):
            raise ValueError(
                "Concatenated prompt-response and full prompt-response input ids should have the same length."
            )

        # Adjust start index for the response's token IDs based on prompt tokenization
        response_token_ids_start_idx = len(prompt_input_ids)
        if (
            prompt_input_ids
            != full_tokenized["input_ids"][:response_token_ids_start_idx]
        ):
            response_token_ids_start_idx -= 1

        # Re-extract prompt input IDs and attention mask after adjustment
        prompt_input_ids = full_tokenized["input_ids"][
            :response_token_ids_start_idx
        ]
        prompt_attention_mask = full_tokenized["attention_mask"][
            :response_token_ids_start_idx
        ]

        # Validate length consistency between prompt input IDs and attention mask
        if len(prompt_input_ids) != len(prompt_attention_mask):
            raise ValueError(
                "Prompt input ids and attention mask should have the same length."
            )

        # Re-extract answer input IDs and attention mask after adjustment
        answer_input_ids = full_tokenized["input_ids"][
            response_token_ids_start_idx:
        ]
        answer_attention_mask = full_tokenized["attention_mask"][
            response_token_ids_start_idx:
        ]

        # Return the structured tokenized information as a dictionary
        return {
            "prompt_input_ids": prompt_input_ids,
            "prompt_attention_mask": prompt_attention_mask,
            "input_ids": answer_input_ids,
            "attention_mask": answer_attention_mask,
        }

    def encode(
        self, semantic_data_array: List[Dict]
    ) -> Tuple[List[np.ndarray], Dict]:
        """
        Tokenize and encode the doc for DPO.

        Args:
            doc (tuple): Contains prompt, completion data to encode

        Returns:
            -> Tuple[List[np.ndarray], Dict]: Tuple of encoded features for DPO and dataset stats
        """

        def extract_prompt_chosen_rejected(data):
            prompt = ""
            chosen = ""
            rejected = ""

            for item in data:
                if item['type'] == 'prompt':
                    prompt = item['content'][0]['text'].strip()
                elif item['type'] == 'chosen':
                    chosen = (
                        self.response_delimiter
                        + item['content'][0]['text'].strip()
                    )
                elif item['type'] == 'rejected':
                    rejected = (
                        self.response_delimiter
                        + item['content'][0]['text'].strip()
                    )

            return prompt, chosen, rejected

        prompt, chosen, rejected = extract_prompt_chosen_rejected(
            semantic_data_array
        )

        fields = ['chosen', 'rejected']
        values = [chosen, rejected]

        # Identify which fields are empty
        empty_fields = [
            field for field, value in zip(fields, values) if value == ""
        ]
        doc_field = None
        # Construct the message based on the empty fields
        if len(empty_fields) > 1:  # If two or more fields are empty
            doc_field = " and ".join(empty_fields)
        elif len(empty_fields) == 1:  # If exactly one field is empty
            doc_field = empty_fields[0]

        # Initialize data_stats
        data_stats = {
            "discarded": 0,
            "processed": 1,
            "successful": 0,
            "raw_chars_count": 0,
            "raw_bytes_count": 0,
            "num_pad_tokens": 0,
            "num_masked_tokens": 0,
            "loss_valid_tokens": 0,
            "num_tokens": 0,
            "normalized_chars_count": 0,
            "normalized_bytes_count": 0,
        }

        if doc_field:
            logger.warning(f"{doc_field} is empty. Skipping this doc...")
            data_stats["discarded"] = 1
            return {}, data_stats

        prompt_chosen_input = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": chosen},
        ]

        prompt_rej_input = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": rejected},
        ]

        prompt_chosen = self.tokenizer.apply_chat_template(
            prompt_chosen_input,
            tokenize=False,
            add_generation_prompt=False,
        )
        prompt_rejected = self.tokenizer.apply_chat_template(
            prompt_rej_input,
            tokenize=False,
            add_generation_prompt=False,
        )

        data_stats["raw_chars_count"] = len(prompt_chosen) + len(
            prompt_rejected
        )
        data_stats["raw_bytes_count"] = len(
            prompt_chosen.encode("utf-8")
        ) + len(prompt_rejected.encode("utf-8"))

        data_stats["normalized_chars_count"] = data_stats["raw_chars_count"]
        data_stats["normalized_bytes_count"] = data_stats["raw_bytes_count"]

        # Fix text and detokenize if necessary
        if self.use_ftfy:
            prompt_chosen = ftfy.fix_text(
                prompt_chosen, normalization=self.ftfy_normalizer
            )
            prompt_rejected = ftfy.fix_text(
                prompt_rejected, normalization=self.ftfy_normalizer
            )

        if self.wikitext_detokenize:
            prompt_chosen = wikitext_detokenizer(prompt_chosen)
            prompt_rejected = wikitext_detokenizer(prompt_rejected)

        if self.use_ftfy or self.wikitext_detokenize:
            data_stats["normalized_chars_count"] = len(prompt_chosen) + len(
                prompt_rejected
            )
            data_stats["normalized_bytes_count"] = len(
                prompt_chosen.encode("utf-8")
            ) + len(prompt_rejected.encode("utf-8"))

        # Extract prompt after appying chat template
        last_assistant_index = -1
        # Extract prompt after applying chat template
        last_assistant_index = prompt_chosen.rfind(self.response_delimiter)
        if last_assistant_index == -1:
            data_stats["discarded"] = 1
            logger.warning(
                f"Can't determine prompt from the chosen string. No `chosen` substring found. Skipping this doc..."
            )
            return {}, data_stats

        truncation_mode = "keep_end"
        prompt = prompt_chosen[:last_assistant_index]
        prompt_chosen = (
            prompt_chosen[:last_assistant_index]
            + prompt_chosen[
                last_assistant_index + len(self.response_delimiter) :
            ]
        )
        prompt_rejected = (
            prompt_rejected[:last_assistant_index]
            + prompt_rejected[
                last_assistant_index + len(self.response_delimiter) :
            ]
        )
        if not isinstance(prompt, str):
            raise ValueError(f"prompt should be an str but got {type(prompt)}")
        prompt_tokens = self.tokenize_text(prompt)
        prompt_tokens = {f"prompt_{k}": v for k, v in prompt_tokens.items()}

        chosen_tokens = self.build_tokenized_answer(
            prompt,
            prompt_chosen,
        )

        rejected_tokens = self.build_tokenized_answer(prompt, prompt_rejected)

        # Last prompt token might get merged by tokenizer and
        # it should not be included for generation if that happens
        prompt_len_input_ids = len(prompt_tokens["prompt_input_ids"])

        chosen_prompt_len_input_ids = len(chosen_tokens["prompt_input_ids"])
        rejected_prompt_len_input_ids = len(rejected_tokens["prompt_input_ids"])
        prompt_len_input_ids = min(
            chosen_prompt_len_input_ids, rejected_prompt_len_input_ids
        )

        for k, v in prompt_tokens.items():
            prompt_tokens[k] = v[:prompt_len_input_ids]

        # Make sure prompts only have one different token at most an
        # and length only differs by 1 at most
        num_diff_tokens = sum(
            [
                a != b
                for a, b in zip(
                    chosen_tokens["prompt_input_ids"],
                    rejected_tokens["prompt_input_ids"],
                )
            ]
        )
        num_diff_len = abs(
            chosen_prompt_len_input_ids - rejected_prompt_len_input_ids
        )
        if num_diff_tokens > 1 or num_diff_len > 1:
            logger.warning(f"Num diff tokens in prompts: {num_diff_tokens}")

        # add BOS token to head of prompt
        if not self.add_bos_token:
            prompt_tokens["prompt_input_ids"] = [
                self.bos_token_id
            ] + prompt_tokens["prompt_input_ids"]
            chosen_tokens["prompt_input_ids"] = [
                self.bos_token_id
            ] + chosen_tokens["prompt_input_ids"]
            rejected_tokens["prompt_input_ids"] = [
                self.bos_token_id
            ] + rejected_tokens["prompt_input_ids"]

            prompt_tokens["prompt_attention_mask"] = [1] + prompt_tokens[
                "prompt_attention_mask"
            ]
            chosen_tokens["prompt_attention_mask"] = [1] + chosen_tokens[
                "prompt_attention_mask"
            ]
            rejected_tokens["prompt_attention_mask"] = [1] + rejected_tokens[
                "prompt_attention_mask"
            ]

        # add EOS token to end of answer
        chosen_tokens["input_ids"].append(self.eos_id)
        chosen_tokens["attention_mask"].append(1)
        rejected_tokens["input_ids"].append(self.eos_id)
        rejected_tokens["attention_mask"].append(1)

        longer_response_length = max(
            len(chosen_tokens["input_ids"]), len(rejected_tokens["input_ids"])
        )

        # TODO: Need to revisit the following logic and optimize it.
        # if combined sequence is too long, truncate the prompt
        for answer_tokens in [chosen_tokens, rejected_tokens, prompt_tokens]:
            if (
                len(answer_tokens["prompt_input_ids"]) + longer_response_length
                > self.max_seq_length
            ):
                if truncation_mode == "keep_start":
                    for k in ["prompt_input_ids", "prompt_attention_mask"]:
                        answer_tokens[k] = answer_tokens[k][
                            : self.max_prompt_length
                        ]
                elif truncation_mode == "keep_end":
                    for k in ["prompt_input_ids", "prompt_attention_mask"]:
                        answer_tokens[k] = answer_tokens[k][
                            -self.max_prompt_length :
                        ]
                else:
                    raise ValueError(
                        f"Unknown truncation mode: {truncation_mode}"
                    )

        # if that's still too long, truncate the response
        for answer_tokens in [chosen_tokens, rejected_tokens]:
            if (
                len(answer_tokens["prompt_input_ids"]) + longer_response_length
                > self.max_seq_length
            ):
                for k in ["input_ids", "attention_mask"]:
                    answer_tokens[k] = answer_tokens[k][
                        : self.max_seq_length - self.max_prompt_length
                    ]

        # Create labels
        chosen_sequence_tokens = {
            k: chosen_tokens[f"prompt_{k}"] + chosen_tokens[k]
            for k in ["input_ids", "attention_mask"]
        }
        rejected_sequence_tokens = {
            k: rejected_tokens[f"prompt_{k}"] + rejected_tokens[k]
            for k in ["input_ids", "attention_mask"]
        }

        chosen_sequence_tokens["labels"] = chosen_sequence_tokens["input_ids"][
            1:
        ]
        chosen_sequence_tokens["labels"][
            : len(chosen_tokens["prompt_input_ids"]) - 1
        ] = [self.pad_id] * (len(chosen_tokens["prompt_input_ids"]) - 1)
        rejected_sequence_tokens["labels"] = rejected_sequence_tokens[
            "input_ids"
        ][1:]
        rejected_sequence_tokens["labels"][
            : len(rejected_tokens["prompt_input_ids"]) - 1
        ] = [self.pad_id] * (len(rejected_tokens["prompt_input_ids"]) - 1)

        batch = {}
        total_pad_tokens = 0
        for k, toks in {
            "chosen_": chosen_sequence_tokens,
            "rejected_": rejected_sequence_tokens,
        }.items():
            for type_key, tokens in toks.items():
                ### HF logic doesn't pad the resulting sequences, Seems like
                ### they handle it internally in a different flow outside DPOTrainer
                ### For our use case, we create an empty list of the size of 'sequence_length'
                ### initialized with pad_token_ids
                pad_value = self.pad_id
                if type_key == "token_type_ids":
                    continue
                elif type_key.endswith("attention_mask"):
                    pad_value = 0
                padded_tokens = [pad_value] * self.max_seq_length
                ### Copy over only relevant information to the padded tokens list
                ### Rest of the elements will be pad_token_ids anyway
                ### If we don't align them to the same size, HDF5 conversion fails
                ### since the stack operation expects all data_buffers to the same shape
                padded_tokens[: len(tokens)] = tokens
                total_pad_tokens += self.max_seq_length - len(tokens)
                batch[f"{k}{type_key}"] = padded_tokens

        ## Batch is a dict of 6 items {chosen_input_ids, chosen_attn_mask, chosen_labels, rejected_input_ids, rejected_attn_mask, rejected_labels}
        ## each of which is of length == max_seq_length. Every batch is of (6, max_seq_length) in shape
        # Do not calculate loss on the prompt
        batch['chosen_attention_mask'][
            : (len(chosen_tokens["prompt_input_ids"]) - 1)
        ] = [0] * (len(chosen_tokens["prompt_input_ids"]) - 1)
        batch['rejected_attention_mask'][
            : (len(rejected_tokens["prompt_input_ids"]) - 1)
        ] = [0] * (len(rejected_tokens["prompt_input_ids"]) - 1)

        total_loss_valid_tokens = (
            len(batch['chosen_attention_mask'])
            - (len(chosen_tokens["prompt_input_ids"]) - 1)
            + len(batch['rejected_attention_mask'])
            - (len(rejected_tokens["prompt_input_ids"]) - 1)
        )
        total_masked_tokens = (
            2 * self.max_seq_length
        ) - total_loss_valid_tokens
        # Convert each list in the batch to a numpy array
        for key in batch.keys():
            batch[key] = np.array(batch[key])

        # Stack all elements in the batch dictionary to create a numpy array
        stacked_batch = np.stack(list(batch.values()), axis=0)

        sample = np.expand_dims(stacked_batch, axis=0)
        data_stats.update(
            {
                "successful": 1,
                "num_pad_tokens": total_pad_tokens,
                "num_masked_tokens": total_masked_tokens,
                "loss_valid_tokens": total_loss_valid_tokens,
                "num_tokens": 6 * self.max_seq_length,
            }
        )

        data = {"data": sample}
        return data, data_stats

    def get_token_id(self, token: str) -> int:
        """
        Get the token ID for the given token.

        Args:
            token (str): Token for which the ID is needed.

        Returns:
            int: Token ID.
        """
        return self.tokenizer.convert_tokens_to_ids(token)
