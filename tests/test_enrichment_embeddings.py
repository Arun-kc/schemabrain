"""Tests for schemabrain.enrichment.embeddings.

The Embedder Protocol + FakeEmbedder give us a unit-test surface that
never touches the ONNX runtime. The FastEmbedEmbedder adapter's lazy
loader and tuple-conversion paths are exercised here via a stubbed
`fastembed.TextEmbedding` so we can hit 100% coverage without
downloading a 67MB model in unit tests. A real end-to-end run against
the actual fastembed model is documented in the slice's manual E2E
log.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from schemabrain.core.embedding import ColumnEmbedding
from schemabrain.enrichment.embeddings import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    Embedder,
    FakeEmbedder,
    FastEmbedEmbedder,
    embedding_for,
    fastembed_default,
)


class TestFakeEmbedder:
    def test_implements_embedder_protocol(self) -> None:
        e = FakeEmbedder(dimension=4)
        assert isinstance(e, Embedder)

    def test_embed_returns_correct_dimension(self) -> None:
        e = FakeEmbedder(dimension=8)
        v = e.embed("hello world")
        assert len(v) == 8

    def test_embed_is_deterministic_for_same_text(self) -> None:
        e = FakeEmbedder(dimension=16)
        a = e.embed("customer email address")
        b = e.embed("customer email address")
        assert a == b

    def test_embed_differs_for_different_text(self) -> None:
        e = FakeEmbedder(dimension=16)
        a = e.embed("customer email address")
        b = e.embed("order line item quantity")
        assert a != b

    def test_embed_returns_tuple_of_floats(self) -> None:
        e = FakeEmbedder(dimension=4)
        v = e.embed("anything")
        assert isinstance(v, tuple)
        assert all(isinstance(x, float) for x in v)

    def test_model_property_exposed(self) -> None:
        e = FakeEmbedder(dimension=4, model_name="fake-emb")
        assert e.model_name == "fake-emb"

    def test_dimension_property_exposed(self) -> None:
        e = FakeEmbedder(dimension=12)
        assert e.dimension == 12

    def test_records_calls_for_assertions(self) -> None:
        e = FakeEmbedder(dimension=4)
        e.embed("first")
        e.embed("second")
        assert e.calls == ["first", "second"]

    def test_zero_dimension_rejected(self) -> None:
        with pytest.raises(ValueError, match="dimension must be positive"):
            FakeEmbedder(dimension=0)

    def test_negative_dimension_rejected(self) -> None:
        with pytest.raises(ValueError, match="dimension must be positive"):
            FakeEmbedder(dimension=-1)


class TestEmbeddingFor:
    def test_returns_column_embedding_with_vector_and_metadata(self) -> None:
        e = FakeEmbedder(dimension=8, model_name="fake-emb")
        ce = embedding_for("Customer's email address", embedder=e)
        assert isinstance(ce, ColumnEmbedding)
        assert ce.dimension == 8
        assert len(ce.vector) == 8
        assert ce.model == "fake-emb"

    def test_passes_text_through_to_embedder(self) -> None:
        e = FakeEmbedder(dimension=4)
        embedding_for("hello", embedder=e)
        assert e.calls == ["hello"]

    def test_empty_text_rejected(self) -> None:
        # An embedding of an empty description is meaningless; surface it
        # at the boundary so caller bugs (forgot to enrich first) fail
        # loudly rather than silently storing a zero-vector.
        e = FakeEmbedder(dimension=4)
        with pytest.raises(ValueError, match="text must be non-empty"):
            embedding_for("", embedder=e)

    def test_whitespace_only_text_rejected(self) -> None:
        e = FakeEmbedder(dimension=4)
        with pytest.raises(ValueError, match="text must be non-empty"):
            embedding_for("   \n\t  ", embedder=e)

    def test_dimension_mismatch_from_embedder_raises(self) -> None:
        # If an embedder reports dimension N but actually produces a
        # vector of different length, we want the constructor of
        # ColumnEmbedding to reject it (defense in depth — fastembed
        # could in principle change behavior under us).
        class LiarEmbedder:
            model_name = "liar"
            dimension = 4

            def embed(self, text: str) -> tuple[float, ...]:
                return (0.1, 0.2, 0.3)  # 3 floats, claims 4

        with pytest.raises(ValueError, match="dimension 4 does not match vector length 3"):
            embedding_for("anything", embedder=LiarEmbedder())  # type: ignore[arg-type]


class _StubTextEmbedding:
    """Minimal stand-in for `fastembed.TextEmbedding`.

    `embed(texts)` is a generator that yields one numpy-ish object per
    input — represented here as a tuple of floats so we don't even need
    numpy for the test.

    `init_calls` is class-level so tests can inspect it after the
    instance was constructed inside `FastEmbedEmbedder._ensure_model`.
    Each test resets it via `monkeypatch.setattr` so pytest restores it
    on teardown — safer than manual reset.
    """

    init_calls: list[dict] = []  # noqa: RUF012 — class-level test capture; reset per-test via monkeypatch

    def __init__(self, *, model_name: str, cache_dir: str | None = None) -> None:
        # F1-D added ``cache_dir`` kwarg pass-through. Stub mirrors the
        # real upstream signature (`fastembed.TextEmbedding.__init__`
        # line 82) so the production call shape is exercised.
        type(self).init_calls.append({"model_name": model_name, "cache_dir": cache_dir})
        self._model_name = model_name
        self._cache_dir = cache_dir

    def embed(self, texts):
        for t in texts:
            # Length doesn't have to match DEFAULT_EMBEDDING_DIM here;
            # FastEmbedEmbedder's tuple conversion just trusts the
            # underlying model. Tests that check the cross-cutting
            # ColumnEmbedding shape use a 4-dim FastEmbedEmbedder
            # explicitly.
            yield (0.1 * len(t), 0.2, 0.3, 0.4)


class TestFastEmbedEmbedder:
    def test_default_model_and_dimension(self) -> None:
        e = FastEmbedEmbedder()
        assert e.model_name == DEFAULT_EMBEDDING_MODEL
        assert e.dimension == DEFAULT_EMBEDDING_DIM

    def test_factory_returns_fastembed_embedder(self) -> None:
        assert isinstance(fastembed_default(), FastEmbedEmbedder)

    def test_embed_lazily_loads_model_then_returns_tuple_of_floats(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch where the import happens — fastembed.TextEmbedding —
        # since FastEmbedEmbedder imports it inside _ensure_model.
        # Reset init_calls via monkeypatch so pytest restores it.
        import fastembed

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)
        _stub_huggingface_download(monkeypatch)

        e = FastEmbedEmbedder(dimension=4)
        v = e.embed("hello")

        # Model name + cache_dir were passed to the underlying loader.
        assert len(_StubTextEmbedding.init_calls) == 1
        assert _StubTextEmbedding.init_calls[0]["model_name"] == DEFAULT_EMBEDDING_MODEL
        # cache_dir is the new F1-D contract: never None, always a path.
        assert _StubTextEmbedding.init_calls[0]["cache_dir"] is not None
        # tuple-of-floats at the boundary, not numpy.
        assert isinstance(v, tuple)
        assert all(isinstance(x, float) for x in v)
        assert v == (
            pytest.approx(0.5),  # 0.1 * len("hello") = 0.5
            pytest.approx(0.2),
            pytest.approx(0.3),
            pytest.approx(0.4),
        )

    def test_embed_only_loads_model_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Subsequent embed() calls must reuse the model — otherwise we'd
        # pay the (real) ~10s ONNX load on every call.
        import fastembed

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)
        _stub_huggingface_download(monkeypatch)

        e = FastEmbedEmbedder(dimension=4)
        e.embed("first")
        e.embed("second")
        e.embed("third")

        assert len(_StubTextEmbedding.init_calls) == 1

    def test_implements_embedder_protocol(self) -> None:
        # Constructed instance must satisfy the Protocol.
        e = FastEmbedEmbedder()
        assert isinstance(e, Embedder)

    def test_eq_and_repr_unaffected_by_lazy_model_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression for HIGH 1: two embedders with same model_name +
        # dimension must compare equal regardless of warm-up state, and
        # repr must NOT include the live ONNX object.
        import fastembed

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)
        _stub_huggingface_download(monkeypatch)

        cold = FastEmbedEmbedder(dimension=4)
        warm = FastEmbedEmbedder(dimension=4)
        warm.embed("warm me up")

        assert cold == warm, "warm-up state must not affect equality"
        assert "TextEmbedding" not in repr(warm), "live model object must not leak into repr"
        assert "_model" not in repr(warm)


def _stub_huggingface_download(
    monkeypatch: pytest.MonkeyPatch, *, model_size: int = 70_000_000
) -> list[dict]:
    """Replace ``huggingface_hub.snapshot_download`` with a stub that
    materializes a dummy model file of the requested size.

    F1-D V2 changed ``_ensure_model`` to invoke ``snapshot_download``
    eagerly before constructing ``TextEmbedding``. Tests that exercise
    the embed path now need to short-circuit the real HF Hub call —
    network access in unit tests is wrong, and the real call would
    also surface a ``hf-xet`` ``DeprecationWarning`` that ``filterwarnings
    = ["error"]`` in pyproject promotes to a test failure.

    The stub creates a snapshot dir under the cache dir's mangled
    layout and writes a dummy file with the expected name + minimum
    size so ``_ensure_model_files_present``'s integrity check passes.

    Returns a list that records each call's kwargs for assertions.
    """
    import huggingface_hub

    from schemabrain.enrichment import embeddings

    calls: list[dict] = []

    def _fake_snapshot_download(*, repo_id: str, cache_dir: str, **kwargs) -> str:
        calls.append({"repo_id": repo_id, "cache_dir": cache_dir, **kwargs})
        # Mirror the HF Hub on-disk layout so the integrity check
        # finds the file at the expected path.
        mangled = "models--" + repo_id.replace("/", "--")
        snapshot_dir = Path(cache_dir) / mangled / "snapshots" / "stub-revision-hash"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / "model_optimized.onnx").write_bytes(b"\0" * model_size)
        return str(snapshot_dir)

    # Patch the symbol in the module where it's used — embeddings.py
    # does ``from huggingface_hub import snapshot_download`` inside the
    # function, so patching ``huggingface_hub.snapshot_download`` itself
    # is the right surface.
    monkeypatch.setattr(huggingface_hub, "snapshot_download", _fake_snapshot_download)
    # Also patch on the embeddings module in case a future refactor
    # caches the symbol there. Defensive belt against import shape drift.
    if hasattr(embeddings, "snapshot_download"):  # pragma: no cover - defensive
        monkeypatch.setattr(embeddings, "snapshot_download", _fake_snapshot_download)
    return calls


class TestFastEmbedCacheLocation:
    """F1-D regression: fastembed cache must NOT default to ``$TMPDIR``.

    On macOS the default fastembed cache lives at
    ``$TMPDIR/fastembed_cache/`` which resolves to
    ``/var/folders/.../T/`` — a system-managed directory periodically
    purged by ``periodic`` (~3-day TTL). Once macOS evicts the model,
    the snapshot dir survives as a skeleton but the actual ONNX blob is
    gone, so the next embedding call crashes with ``NoSuchFile`` deep
    inside ONNXRuntime. The fix overrides ``cache_dir`` to a persistent
    user-owned location.
    """

    def test_cache_dir_defaults_to_user_home_not_tmpdir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import fastembed

        from schemabrain.enrichment.embeddings import _FASTEMBED_CACHE_ENV

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)
        _stub_huggingface_download(monkeypatch)
        # Strip any operator override so we exercise the default path.
        monkeypatch.delenv(_FASTEMBED_CACHE_ENV, raising=False)
        # Pin Path.home() so the test doesn't depend on the runner's
        # actual home (CI runs under various $HOME values).
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        FastEmbedEmbedder().embed("warm")

        actual_cache_dir = _StubTextEmbedding.init_calls[0]["cache_dir"]
        assert actual_cache_dir is not None
        # Positive contract: the resolver computes ``Path.home() /
        # ".cache" / "fastembed"`` — anchored to whatever home is
        # (here, the patched ``tmp_path``). The intent is "the cache
        # lives under the user's home dir, not under ``$TMPDIR``" —
        # asserting the positive shape is robust across OSes; a prior
        # version of this test negated against ``os.environ['TMPDIR']``
        # and broke on Linux CI where pytest's ``tmp_path`` itself
        # lives under ``/tmp`` (so ``Path.home() == tmp_path == /tmp/...``
        # by construction). macOS local passed only because pytest
        # tunneled through ``/private/var/folders/...`` which doesn't
        # share a prefix with ``$TMPDIR=/var/folders/...``.
        assert actual_cache_dir == str(tmp_path / ".cache" / "fastembed")

    def test_cache_dir_honors_operator_env_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import fastembed

        from schemabrain.enrichment.embeddings import _FASTEMBED_CACHE_ENV

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)
        _stub_huggingface_download(monkeypatch)
        custom = tmp_path / "ops" / "fastembed-cache"
        monkeypatch.setenv(_FASTEMBED_CACHE_ENV, str(custom))

        FastEmbedEmbedder().embed("warm")

        actual_cache_dir = _StubTextEmbedding.init_calls[0]["cache_dir"]
        assert actual_cache_dir == str(custom)
        # Directory was created on the way in (fastembed itself will
        # mkdir but we materialize early so the heal step can list it).
        assert custom.is_dir()


class TestFastEmbedHFEagerDownload:
    """F1-D V2 regression: the eager ``huggingface_hub.snapshot_download``
    must fire BEFORE ``TextEmbedding`` constructs, with the right
    repo_id, allow_patterns narrow enough to skip unquantized variants,
    and an integrity check that catches the zero-byte partial-download
    case fastembed's own loader does not surface.
    """

    def test_eager_fetch_passes_canonical_repo_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import fastembed

        from schemabrain.enrichment.embeddings import _FASTEMBED_CACHE_ENV

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)
        monkeypatch.setenv(_FASTEMBED_CACHE_ENV, str(tmp_path))
        calls = _stub_huggingface_download(monkeypatch)

        FastEmbedEmbedder().embed("warm")

        # Canonical HF Hub repo for the default bge-small-en-v1.5 model,
        # per fastembed's onnx_embedding registry.
        assert calls[0]["repo_id"] == "qdrant/bge-small-en-v1.5-onnx-q"
        assert calls[0]["cache_dir"] == str(tmp_path)
        # allow_patterns must scope to model + tokenizer files; without
        # this we'd pull a giant unquantized model alongside the
        # quantized one we actually want.
        assert "*.onnx" in calls[0]["allow_patterns"]

    def test_integrity_check_fails_on_zero_byte_model(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The smoke caught fastembed's auto-download producing a 0-byte
        ``model_optimized.onnx`` blob. ``_ensure_model_files_present``
        must catch this case and raise rather than letting the loader
        fail downstream with a confusing NoSuchFile.
        """
        import fastembed

        from schemabrain.enrichment.embeddings import (
            _FASTEMBED_CACHE_ENV,
            EmbeddingUnavailableError,
        )

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)
        monkeypatch.setenv(_FASTEMBED_CACHE_ENV, str(tmp_path))
        # Stub produces a tiny file (1 KB) — well below the 10 MB floor.
        _stub_huggingface_download(monkeypatch, model_size=1024)

        with pytest.raises(EmbeddingUnavailableError, match="downloaded to 1024 bytes"):
            FastEmbedEmbedder().embed("warm")

    def test_integrity_check_wipes_zero_byte_blob_for_retry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """After a corrupt download, the zero-byte file is unlinked so
        the next call doesn't see it present and skip re-fetching.
        """
        import fastembed

        from schemabrain.enrichment.embeddings import (
            _FASTEMBED_CACHE_ENV,
            EmbeddingUnavailableError,
        )

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)
        monkeypatch.setenv(_FASTEMBED_CACHE_ENV, str(tmp_path))
        _stub_huggingface_download(monkeypatch, model_size=1024)

        with pytest.raises(EmbeddingUnavailableError):
            FastEmbedEmbedder().embed("warm")

        # The 1 KB blob was wiped so the next snapshot_download call
        # starts from a clean state.
        mangled = "models--qdrant--bge-small-en-v1.5-onnx-q"
        model_file = (
            tmp_path / mangled / "snapshots" / "stub-revision-hash" / "model_optimized.onnx"
        )
        assert not model_file.exists(), "corrupt blob must be unlinked after detection"

    def test_unknown_model_skips_eager_fetch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A custom model not in ``_HF_REPO_MAP`` falls through to
        fastembed's own download path (we don't know the repo mapping).
        """
        import fastembed

        from schemabrain.enrichment.embeddings import _FASTEMBED_CACHE_ENV

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)
        monkeypatch.setenv(_FASTEMBED_CACHE_ENV, str(tmp_path))
        calls = _stub_huggingface_download(monkeypatch)

        FastEmbedEmbedder(model_name="custom/unknown-model").embed("warm")

        # No HF Hub call fired — the unknown model wasn't pre-fetched.
        assert calls == []
        # And fastembed was still constructed — the eager-fetch is a
        # belt-and-braces step, not a gate.
        assert len(_StubTextEmbedding.init_calls) == 1

    def test_hf_download_failure_raises_embedding_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A network error or repo-gone error inside snapshot_download
        must classify as ``EmbeddingUnavailableError`` so the MCP
        wrapper produces a recovery envelope steering the agent at
        ``list_entities``.
        """
        import fastembed
        import huggingface_hub

        from schemabrain.enrichment.embeddings import (
            _FASTEMBED_CACHE_ENV,
            EmbeddingUnavailableError,
        )

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)
        monkeypatch.setenv(_FASTEMBED_CACHE_ENV, str(tmp_path))

        def _network_down(*args, **kwargs):
            raise ConnectionError("HF Hub unreachable")

        monkeypatch.setattr(huggingface_hub, "snapshot_download", _network_down)

        with pytest.raises(EmbeddingUnavailableError, match="ConnectionError"):
            FastEmbedEmbedder().embed("warm")


class TestFastEmbedFailureClassification:
    """F1-E + F1-F regression: ``FastEmbedEmbedder.embed`` must
    convert raw fastembed/ONNX failures into ``EmbeddingUnavailableError``
    so the MCP wrapper can return a recovery envelope steering the
    agent to ``list_entities``. Already-classified errors must
    bubble unchanged (don't double-wrap).
    """

    def test_already_classified_error_bubbles_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If something downstream raises ``EmbeddingUnavailableError``
        directly (e.g. a future check inside the fastembed shim), the
        outer try/except must let it pass through unwrapped — no
        ``EmbeddingUnavailableError → EmbeddingUnavailableError``
        nested chain on the audit log.
        """
        import fastembed

        from schemabrain.enrichment.embeddings import EmbeddingUnavailableError

        class _AlreadyClassified:
            def __init__(self, *, model_name: str, cache_dir: str | None = None) -> None:
                # Raise the classified error AT CONSTRUCTION TIME so the
                # `_ensure_model` path surfaces it pre-embed (mimics what
                # a future preflight check inside the shim would do).
                raise EmbeddingUnavailableError("already classified at preflight")

        monkeypatch.setattr(fastembed, "TextEmbedding", _AlreadyClassified)
        _stub_huggingface_download(monkeypatch)

        with pytest.raises(EmbeddingUnavailableError, match="already classified"):
            FastEmbedEmbedder().embed("query")

    def test_generic_runtime_error_classified_as_embedding_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raw ``RuntimeError`` from ONNX (e.g. NoSuchFile) gets
        promoted to ``EmbeddingUnavailableError`` with the original
        exception chained as ``__cause__`` for operator debugging.
        """
        import fastembed

        from schemabrain.enrichment.embeddings import EmbeddingUnavailableError

        class _OnnxBoom:
            def __init__(self, *, model_name: str, cache_dir: str | None = None) -> None:
                pass

            def embed(self, texts):
                raise RuntimeError("ONNX session init failed: NoSuchFile")

        monkeypatch.setattr(fastembed, "TextEmbedding", _OnnxBoom)
        _stub_huggingface_download(monkeypatch)

        with pytest.raises(EmbeddingUnavailableError, match="RuntimeError") as exc_info:
            FastEmbedEmbedder().embed("query")
        # Chain preserved so operator triage can see the actual ONNX
        # error in server logs.
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert "NoSuchFile" in str(exc_info.value.__cause__)


class TestFastEmbedPartialDownloadHealing:
    """F1-D regression: a half-finished model download leaves the
    snapshot dir intact with an ``*.incomplete`` blob orphan. fastembed
    then loads symlinks pointing at a non-existent file and crashes
    with ``NoSuchFile`` deep inside ONNXRuntime. The heal step wipes
    the model tree so fastembed re-fetches transparently on the next
    construction.
    """

    @staticmethod
    def _seed_partial_cache(cache_dir: Path, model_name: str) -> Path:
        """Create a snapshot dir shaped exactly like HF Hub's partial
        download state — config + tokenizer blobs present, model blob
        present as ``*.incomplete``. Returns the model-root directory.
        """
        mangled = "models--" + model_name.replace("/", "--")
        model_root = cache_dir / mangled
        blobs = model_root / "blobs"
        blobs.mkdir(parents=True)
        # Two complete blobs (config + tokenizer).
        (blobs / "aaaa1111").write_bytes(b'{"hidden_size": 384}')
        (blobs / "bbbb2222").write_bytes(b'{"tokenizer": "..."}')
        # One incomplete blob — the actual 67MB ONNX model.
        (blobs / "cccc3333.incomplete").write_bytes(b"partial")
        return model_root

    def test_heal_wipes_model_tree_when_incomplete_blob_present(self, tmp_path: Path) -> None:
        from schemabrain.enrichment.embeddings import _heal_partial_download

        model_root = self._seed_partial_cache(tmp_path, DEFAULT_EMBEDDING_MODEL)
        assert model_root.is_dir()

        _heal_partial_download(tmp_path, DEFAULT_EMBEDDING_MODEL)

        # Whole model tree gone — fastembed will re-download from scratch.
        assert not model_root.exists()

    def test_heal_leaves_clean_cache_alone(self, tmp_path: Path) -> None:
        """A complete cache (no ``*.incomplete`` orphans) must survive
        the heal step unchanged — we only wipe on actual corruption.
        """
        from schemabrain.enrichment.embeddings import _heal_partial_download

        mangled = "models--" + DEFAULT_EMBEDDING_MODEL.replace("/", "--")
        model_root = tmp_path / mangled
        blobs = model_root / "blobs"
        blobs.mkdir(parents=True)
        (blobs / "complete1").write_bytes(b"data")
        (blobs / "complete2").write_bytes(b"data")

        _heal_partial_download(tmp_path, DEFAULT_EMBEDDING_MODEL)

        assert model_root.is_dir()
        assert (blobs / "complete1").exists()
        assert (blobs / "complete2").exists()

    def test_heal_is_noop_on_missing_cache(self, tmp_path: Path) -> None:
        """Cold-start path: cache_dir exists (we mkdir it) but no model
        subtree yet. Heal must not crash.
        """
        from schemabrain.enrichment.embeddings import _heal_partial_download

        # tmp_path is empty — first-time install case.
        _heal_partial_download(tmp_path, DEFAULT_EMBEDDING_MODEL)
        # No crash, no side effects.
        assert list(tmp_path.iterdir()) == []

    def test_heal_is_noop_when_model_root_exists_but_blobs_dir_does_not(
        self, tmp_path: Path
    ) -> None:
        """Edge case: model_root directory exists (perhaps a stub left
        behind by an aborted ``snapshots`` walk) but the ``blobs/``
        subdirectory has never been created. Nothing to wipe — just
        return without crashing on the missing path.
        """
        from schemabrain.enrichment.embeddings import _heal_partial_download

        mangled = "models--" + DEFAULT_EMBEDDING_MODEL.replace("/", "--")
        model_root = tmp_path / mangled
        model_root.mkdir(parents=True)
        # Stub a ``snapshots/`` entry but no ``blobs/`` — the partial-
        # download check should bail at the ``blobs.is_dir()`` guard.
        (model_root / "snapshots").mkdir()

        _heal_partial_download(tmp_path, DEFAULT_EMBEDDING_MODEL)

        # Model tree preserved; the heal step had nothing to act on.
        assert model_root.is_dir()
        assert (model_root / "snapshots").is_dir()

    def test_heal_fires_via_ensure_model_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Integration: ``_ensure_model`` invokes ``_heal_partial_download``
        before constructing ``TextEmbedding``. A partial cache present at
        construction time gets wiped, and fastembed sees a clean state.
        """
        import fastembed

        from schemabrain.enrichment.embeddings import _FASTEMBED_CACHE_ENV

        monkeypatch.setattr(_StubTextEmbedding, "init_calls", [])
        monkeypatch.setattr(fastembed, "TextEmbedding", _StubTextEmbedding)
        monkeypatch.setenv(_FASTEMBED_CACHE_ENV, str(tmp_path))
        _stub_huggingface_download(monkeypatch)

        # Seed the cache as if a prior partial download left orphans.
        model_root = TestFastEmbedPartialDownloadHealing._seed_partial_cache(
            tmp_path, DEFAULT_EMBEDDING_MODEL
        )
        assert model_root.is_dir()

        FastEmbedEmbedder().embed("warm")

        # Heal ran: model tree wiped before TextEmbedding constructed.
        assert not model_root.exists()
        # TextEmbedding still got constructed (mocked) so the smoke path is alive.
        assert len(_StubTextEmbedding.init_calls) == 1
