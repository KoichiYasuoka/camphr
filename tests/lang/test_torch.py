import camphr.lang.juman as juman
import camphr.lang.mecab as mecab
import pytest
import spacy
import torch


@pytest.mark.parametrize("cls", [mecab.TorchJapanese, juman.TorchJapanese])
def test_torchlang_serialization(cls, tmpdir):
    nlp = cls()
    nlp.to(torch.device("cpu"))
    nlp.to_disk(str(tmpdir))
    nlp = spacy.load(str(tmpdir))
    assert isinstance(nlp, cls)
    nlp.to(torch.device("cpu"))
