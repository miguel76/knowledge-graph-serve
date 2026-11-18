import io
from pathlib import Path

from kgserve import hashindex, rdffiles
from kgserve.hashindex import HashIndex

NQ = (b"# a comment with <http://ex.org/c#comment>\n"
      b"<http://ex.org/doc#a> <http://ex.org/p> \"x\" <http://ex.org/g> .\n"
      b"<http://ex.org/doc#b> <http://ex.org/p> <http://ex.org/doc#c> <http://ex.org/g> .\n"
      b"_:b0 <http://ex.org/p> <http://ex.org/other#d> .\n"
      b"<http://ex.org/plain> <http://ex.org/p> \"<http://ex.org/lit#e>\" .\n"
      b"  <http://ex.org/doc2#f> <http://ex.org/p> \"no newline at the end\" .")


def items(tmp_path: Path, name: str, content: bytes) -> list[rdffiles.LoadItem]:
    (tmp_path / name).write_bytes(content)
    return rdffiles.load_items(name, tmp_path / name, lambda key: None)


def test_line_tap_collects_subjects_across_chunk_boundaries(tmp_path):
    for chunk in (1, 3, 7, 64, 1 << 16):  # IRIs split at every possible position
        index = HashIndex()
        [item] = items(tmp_path, "kg.nq", NQ)
        stream = index.tap(item, item.open())
        out = io.BytesIO()
        while data := stream.read(chunk):
            out.write(data)
        assert out.getvalue() == NQ  # the store gets the content unchanged
        assert index.finish() == {"http://ex.org/doc": ["http://ex.org/doc#a", "http://ex.org/doc#b"],
                                  "http://ex.org/doc2": ["http://ex.org/doc2#f"]}, chunk


def test_other_formats_are_parsed(tmp_path):
    index = HashIndex()
    [item] = items(tmp_path, "void.ttl", b"@prefix : <http://ex.org/d#> .\n:x :p :y .\n<http://ex.org/e> :p 1 .\n")
    assert index.tap(item, item.open()) is not None
    assert index.finish() == {"http://ex.org/d": ["http://ex.org/d#x"]}


def test_large_files_in_other_formats_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(hashindex, "MAX_PARSE_SIZE", 10)
    index = HashIndex()
    [item] = items(tmp_path, "big.ttl", b"<http://ex.org/d#x> <http://ex.org/p> 1 .\n")
    index.tap(item, item.open())
    assert index.finish() == {}
