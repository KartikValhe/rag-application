# Synthetic embedding model (tests only)

`tests/fixtures/ml.py` BUILDS a tiny tokenizer (`tokenizer.json`, word level, about 90 words in English, Hindi and
Marathi) and a tiny ONNX graph (8 dimensions) in a temporary directory at test time, using the `tokenizers` library
and the dev-only `onnx` package. Nothing is checked in as a binary and nothing is downloaded.

The graph is `reverse_cumsum(embedding_table[input_ids] * attention_mask) + embedding_table[input_ids]`:

- position 0 depends on every real token (so CLS pooling is meaningful),
- padding never changes the values at real positions (so a batched run equals a single-item run), and
- padding positions are non-zero (so pooling that ignores the mask gives a different vector).

It exposes the same inputs (`input_ids`, `attention_mask`, optionally `token_type_ids`) and the same output name
(`last_hidden_state`) as the real XLM-R based models, so the pooling, normalisation, batching, truncation and
loader code runs against a real onnxruntime session and a real `tokenizers` tokenizer.

It says nothing about the quality of the real model. That is what `python -m app.scripts.check_onnx_parity` and
the `real_model` test (`RUN_REAL_MODEL=1`) are for.

`parity_sentences.txt` holds the 50 sentences (English, Hindi, Marathi; written for this project, not copied from
any source) used by `python -m app.scripts.check_onnx_parity`.
