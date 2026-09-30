"""Text -> vector, behind one small interface.

The default, HashingEmbedder, needs no model download and no network: it
hashes words, word pairs and character 4-grams into a fixed-size vector. It
is a lexical retriever, not a semantic one -- "why is the left ventricle
thicker" finds the passage that says "left ventricle ... thicker wall" -- and
on a curated corpus of a few hundred passages that is what is needed. The
character n-grams make it forgive speech-recognition misspellings
("mitocondria").

SentenceTransformerEmbedder is used instead when the package is installed and
EMBEDDER=sentence_transformers; it is too big to be the default on a Pi.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import numpy as np

STOPWORDS = set("""a an the of to in on at is are was were be been being it its this that these those
and or but if then so as for with by from about into over under what which who whom whose why how
when where do does did can could should would will shall may might must i you he she we they me
him her us them my your his our their there here than very also just not no yes""".split())

RE_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def tokens(text: str) -> list[str]:
    words = [w for w in RE_WORD.findall(text.lower()) if w not in STOPWORDS]
    return [_stem(w) for w in words]


def _stem(word: str) -> str:
    for suffix in ("ies", "es", "s"):
        if word.endswith(suffix) and len(word) > len(suffix) + 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashingEmbedder:
    name = "hashing-v1"

    def __init__(self, dim: int = 2048):
        self.dim = dim

    def _index(self, feature: str) -> tuple[int, float]:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "little")
        return value % self.dim, (1.0 if (value >> 63) & 1 else -1.0)

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            words = tokens(text)
            feats: dict[str, float] = {}
            for w in words:
                feats[f"w:{w}"] = feats.get(f"w:{w}", 0.0) + 1.0
                padded = f"<{w}>"
                for i in range(max(1, len(padded) - 3)):
                    g = f"c:{padded[i:i + 4]}"
                    feats[g] = feats.get(g, 0.0) + 0.25
            for a, b in zip(words, words[1:]):
                feats[f"b:{a}_{b}"] = feats.get(f"b:{a}_{b}", 0.0) + 1.5
            for feat, count in feats.items():
                idx, sign = self._index(feat)
                out[row, idx] += sign * (1.0 + math.log(count)) if count >= 1 else sign * count
            norm = np.linalg.norm(out[row])
            if norm:
                out[row] /= norm
        return out


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer   # optional dependency
        self._model = SentenceTransformer(model_name)
        self.name = f"st:{model_name}"
        self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> np.ndarray:
        vecs = self._model.encode(texts, normalize_embeddings=True)
        return np.asarray(vecs, dtype=np.float32)


def make_embedder(kind: str) -> Embedder:
    if kind == "sentence_transformers":
        try:
            return SentenceTransformerEmbedder()
        except ImportError:
            pass
    return HashingEmbedder()
