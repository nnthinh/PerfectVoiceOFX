# SpeechBrain ECAPA-TDNN speaker encoder

| | |
| --- | --- |
| Code reference | [speechbrain/speechbrain](https://github.com/speechbrain/speechbrain) `speechbrain/lobes/models/ECAPA_TDNN.py`, `speechbrain/lobes/features.py` |
| Code license | Apache License 2.0 |
| Weights | [`speechbrain/spkrec-ecapa-voxceleb`](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb) → `embedding_model.ckpt` (~83 MB) |
| Weights license | Apache License 2.0 (model card) |
| Training data | VoxCeleb 1 + VoxCeleb 2 |
| Paper | Desplanques, Thienpondt, Demuynck — *ECAPA-TDNN: Emphasized Channel Attention, Propagation and Aggregation in TDNN Based Speaker Verification*, Interspeech 2020 |

## How PerfectVoice uses it

- `engine/perfectvoice_engine/tse/encoder.py` is a dependency-free
  re-implementation of the SpeechBrain modules. Module names match the
  checkpoint so it loads with `strict=True` and `torch.load(weights_only=True)`.
- `tests/unit/test_tse.py` pins the checkpoint's tensor names and shapes
  (`tests/fixtures/ecapa_voxceleb_state_dict.json`) and, when `speechbrain`
  is installed, checks numerical parity of features and embeddings.
- The checkpoint is fetched only on user click (`POST /v1/models/download`
  with `{"name": "ecapa_voxceleb"}`; the panel chains it after the main
  model) and verified against a pinned sha256 or the Hub's `X-Linked-Etag`.

Apache License 2.0: https://www.apache.org/licenses/LICENSE-2.0
