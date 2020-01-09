"""Module trf_model defines pytorch-transformers components."""
import dataclasses
import pickle
from pathlib import Path
from typing import List, Optional, Sequence, Union

import numpy as np
import spacy
import spacy.language
import torch
import transformers as trf
from camphr.pipelines.trf_utils import ATTRS
from camphr.pipelines.utils import get_similarity
from camphr.torch_utils import (
    OptimizerParameters,
    TensorWrapper,
    TorchPipe,
    get_parameters_with_decay,
)
from camphr.utils import zero_pad
from spacy.gold import GoldParse
from spacy.language import Language
from spacy.tokens import Doc
from transformers.modeling_bert import BERT_INPUTS_DOCSTRING
from transformers.modeling_xlnet import XLNET_INPUTS_DOCSTRING

spacy.language.ENABLE_PIPELINE_ANALYSIS = True
PRETRAINED_MODEL_ARCHIVE_MAP = {
    "bert-ja-juman": "s3://camphr/trf_models/bert/bert-ja-juman.bin",
    "xlnet-ja": "s3://camphr/trf_models/xlnet/pytorch_model.bin",
}
PRETRAINED_CONFIG_ARCHIVE_MAP = {
    "bert-ja-juman": "s3://camphr/trf_models/bert/bert-ja-juman-config.json",
    "xlnet-ja": "s3://camphr/trf_models/xlnet/config.json",
}


@dataclasses.dataclass
class TransformersModelInputsBase:
    input_ids: torch.Tensor
    token_type_ids: Optional[torch.Tensor] = None
    attention_mask: Optional[torch.Tensor] = None
    head_mask: Optional[torch.Tensor] = None
    __doc__ = BERT_INPUTS_DOCSTRING


@dataclasses.dataclass
class BertModelInputs(TransformersModelInputsBase):
    position_ids: Optional[torch.Tensor] = None
    __doc__ = BERT_INPUTS_DOCSTRING


@dataclasses.dataclass
class XLNetModelInputs(TransformersModelInputsBase):
    mems: Optional[List[torch.FloatTensor]] = None
    perm_mask: Optional[torch.FloatTensor] = None
    target_mapping: Optional[torch.FloatTensor] = None
    __doc__ = XLNET_INPUTS_DOCSTRING


TransformersModelInputs = Union[BertModelInputs, XLNetModelInputs]


@dataclasses.dataclass
class BertModelOutputs:
    """A container for BertModel outputs. See `trf.BertModel`'s docstring for detail."""

    laste_hidden_state: torch.FloatTensor  # shape ``(batch_size, sequence_length, hidden_size)``
    pooler_output: torch.FloatTensor  # shape ``(batch_size, hidden_size)``
    hidden_states: Optional[torch.FloatTensor] = None
    # list of (one for the output of each layer + the output of the embeddings) of shape ``(batch_size, sequence_length, hidden_size)``
    attensions: Optional[torch.FloatTensor] = None
    # list of (one for each layer) of shape ``(batch_size, num_heads, sequence_length, sequence_length)``


@dataclasses.dataclass
class XLNetModelOutputs:
    """A container for trf.XLNetModel outputs. See `trf.XLNetModel`'s docstring for detail."""

    laste_hidden_state: torch.FloatTensor  # shape ``(batch_size, sequence_length, hidden_size)``
    mems: Optional[List[torch.FloatTensor]] = None
    hidden_states: Optional[torch.FloatTensor] = None
    # list of (one for the output of each layer + the output of the embeddings) of shape ``(batch_size, sequence_length, hidden_size)``
    attensions: Optional[torch.FloatTensor] = None
    # list of (one for each layer) of shape ``(batch_size, num_heads, sequence_length, sequence_length)``


TransformerModelOutputs = Union[BertModelOutputs, XLNetModelOutputs]


class MODEL_NAMES:
    bert = "bert"
    xlnet = "xlnet"


class CLS_NAMES:
    inputs = "inputs"
    outputs = "outputs"
    model = "model"
    config = "config"


def get_trf_name(name: str) -> str:
    for k in {MODEL_NAMES.bert, MODEL_NAMES.xlnet}:
        if k in name:
            return k
    raise ValueError(f"Illegal model name: {name}")


class TransformersModel(TorchPipe):
    """Pytorch transformers Model component.

    Attach the model outputs to doc.
    """

    def __init__(self, model=True, **cfg):
        self.model = model
        self.cfg = cfg

    @classmethod
    def from_nlp(cls, nlp, **cfg):
        return cls(**cfg)

    @classmethod
    def from_pretrained(cls, name_or_path: str, **cfg):
        """Load pretrained model."""
        cfg["trf_name"] = name_or_path
        model = cls.Model(from_pretrained=True, **cfg)
        cfg["trf_config"] = dict(model.config.to_dict())
        return cls(model=model, **cfg)

    @classmethod
    def Model(cls, **cfg) -> trf.PreTrainedModel:
        """Create trf Model"""
        if cfg.get("from_pretrained"):
            trf_name = cfg.get("trf_name", "")
            model = cls.trf_model_cls.from_pretrained(trf_name)
        else:
            if "vocab_size" in cfg["trf_config"]:
                vocab_size = cfg["trf_config"]["vocab_size"]
                cfg["trf_config"]["vocab_size_or_config_json_file"] = vocab_size
            model = cls.trf_model_cls(cls.trf_config_cls(**cfg["trf_config"]))
        return model

    @property
    def max_length(self) -> int:
        return self.model.config.max_position_embeddings

    def predict(self, docs: List[Doc]) -> TransformerModelOutputs:
        self.require_model()
        self.model.eval()
        x = self.docs_to_trfinput(docs)
        with torch.no_grad():
            y = self.output_cls(*self.model(**dataclasses.asdict(x)))
        return y

    def set_annotations(
        self, docs: List[Doc], outputs: TransformerModelOutputs, set_vector: bool = True
    ) -> None:
        """Assign the extracted features to the Doc.

        Args:
            set_vector: If True, attach vector to doc. This may harms the performance.
        """
        for i, doc in enumerate(docs):
            length = len(doc._.get(ATTRS.token_ids))
            # Instead of assigning tensor directory, assign `TensorWrapper`
            # so that trailing pipe can handle batch tensor efficiently.
            doc._.set(
                ATTRS.last_hidden_state,
                TensorWrapper(outputs.laste_hidden_state, i, length),
            )

            if set_vector:
                lh: torch.Tensor = doc._.get(ATTRS.last_hidden_state).get()
                doc_tensor = lh.new_zeros((len(doc), lh.shape[-1]))
                # TODO: Inefficient
                # TODO: Store the functionality into user_hooks after https://github.com/explosion/spaCy/issues/4439 was released
                for i, a in enumerate(doc._.get(ATTRS.align)):
                    if self.max_length > 0:
                        a = [aa for aa in a if aa < len(lh)]
                    doc_tensor[i] += lh[a].sum(0)
                doc.tensor = doc_tensor
                doc.user_hooks["vector"] = get_doc_vector_via_tensor
                doc.user_span_hooks["vector"] = get_span_vector_via_tensor
                doc.user_token_hooks["vector"] = get_token_vector_via_tensor
                doc.user_hooks["similarity"] = get_similarity
                doc.user_span_hooks["similarity"] = get_similarity
                doc.user_token_hooks["similarity"] = get_similarity

    @property
    def freeze(self) -> bool:
        if self.cfg.get("freeze"):
            return True
        return False

    def update(self, docs: List[Doc], golds: List[GoldParse]):
        """Simply forward docs in training mode."""
        self.require_model()
        x = self.docs_to_trfinput(docs)
        if self.freeze:
            torch.set_grad_enabled(False)
            self.model.eval()
        else:
            self.model.train()
        y = self.output_cls(*self.model(**dataclasses.asdict(x)))
        torch.set_grad_enabled(True)
        # set_vector=False because vector may not be necessary in updating.
        # You can still use model outputs via doc._.transformers_last_hidden_state etc.
        self.set_annotations(docs, y, set_vector=False)

    def docs_to_trfinput(self, docs: Sequence[Doc]) -> TransformersModelInputs:
        """Generate input data for trf model from docs."""
        token_ids_list = [doc._.get(ATTRS.token_ids) for doc in docs]
        inputs = self.input_cls(
            input_ids=torch.tensor(zero_pad(token_ids_list), device=self.device)
        )
        inputs.attention_mask = torch.tensor(
            zero_pad([doc._.get(ATTRS.attention_mask) for doc in docs]),
            device=self.device,
        )
        inputs.token_type_ids = torch.tensor(
            zero_pad([doc._.get(ATTRS.attention_mask) for doc in docs]),
            device=self.device,
        )
        return inputs

    def optim_parameters(self) -> OptimizerParameters:
        if self.freeze:
            return []
        no_decay = self.cfg.get("no_decay")
        weight_decay = self.cfg.get("weight_decay")
        return get_parameters_with_decay(self.model, no_decay, weight_decay)

    def to_disk(self, path: Path, exclude=tuple(), **kwargs):
        path.mkdir(exist_ok=True)
        model: trf.PreTrainedModel = self.model
        model.save_pretrained(str(path))
        with (path / "cfg.pkl").open("wb") as f:
            pickle.dump(self.cfg, f)

    def from_disk(self, path: Path, exclude=tuple(), **kwargs) -> "TransformersModel":
        with (path / "cfg.pkl").open("rb") as f:
            self.cfg = pickle.load(f)
        self.model = self.trf_model_cls.from_pretrained(str(path))
        return self


@spacy.component("bert", assigns=[f"doc._.{ATTRS.last_hidden_state}"])
class BertModel(TransformersModel):
    output_cls = BertModelOutputs
    input_cls = BertModelInputs
    trf_model_cls = trf.BertModel
    trf_config_cls = trf.BertConfig


@spacy.component("xlnet", assigns=[f"doc._.{ATTRS.last_hidden_state}"])
class XLNetModel(TransformersModel):
    trf_model_cls = trf.XLNetModel
    trf_config_cls = trf.XLNetConfig
    input_cls = XLNetModelInputs
    output_cls = XLNetModelOutputs


def get_doc_vector_via_tensor(doc) -> np.ndarray:
    return doc.tensor.sum(0).cpu().numpy()


def get_span_vector_via_tensor(span) -> np.ndarray:
    return span.doc.tensor[span.start : span.end].sum(0).cpu().numpy()


def get_token_vector_via_tensor(token) -> np.ndarray:
    return token.doc.tensor[token.i].cpu().numpy()


Language.factories[BertModel.name] = BertModel
Language.factories[XLNetModel.name] = XLNetModel