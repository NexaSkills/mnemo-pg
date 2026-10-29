#!/usr/bin/env python3
"""Scan the vendored core for sqlite-isms the native layer must answer.

Lists SQL literals (from string constants) that use idioms PG cannot
execute natively: MATCH, rowid, PRAGMA, INSERT OR ..., last_insert_rowid,
strftime/julianday, ATTACH, GLOB, x'..' blobs, CREATE VIRTUAL TABLE.
Groups hits per file so a patch pass has an exact worklist.
"""
import re
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent / "vendor" / "mnemosyne"

PATTERNS = {
    "fts_match": re.compile(r"\bMATCH\b", re.I),
    "rowid": re.compile(r"\browid\b", re.I),
    "pragma": re.compile(r"\bPRAGMA\b", re.I),
    "insert_or": re.compile(r"\bINSERT\s+OR\s+(REPLACE|IGNORE|ROLLBACK|ABORT|FAIL)\b", re.I),
    "last_insert_rowid": re.compile(r"\blast_insert_rowid\s*\(", re.I),
    "strftime": re.compile(r"\bstrftime\s*\(", re.I),
    "julianday": re.compile(r"\bjulianday\s*\(", re.I),
    "datetime_fn": re.compile(r"\bdatetime\s*\(", re.I),
    "attach": re.compile(r"\bATTACH\b", re.I),
    "glob": re.compile(r"\bGLOB\b", re.I),
    "hexblob": re.compile(r"x'[0-9A-Fa-f]*'"),
    "virtual_table": re.compile(r"CREATE\s+VIRTUAL\s+TABLE", re.I),
    "autoincrement": re.compile(r"\bAUTOINCREMENT\b", re.I),
    "randomblob": re.compile(r"\brandomblob\s*\(", re.I),
    "typeof": re.compile(r"\btypeof\s*\(", re.I),
    "ifnull": re.compile(r"\bifnull\s*\(", re.I),
    "insert_select": re.compile(r"\bINSERT\b[\s\S]{0,200}?\bSELECT\b", re.I),
    "update_from": re.compile(r"\bUPDATE\b[\s\S]{0,120}?\bFROM\b", re.I),
    "delete_using": re.compile(r"\bDELETE\b[\s\S]{0,80}?\bUSING\b", re.I),
}
# Only look inside string literals so prose/comments don't count.
STR = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|\"(?:[^\"\\]|\\.)*\"")


def main():
    counts = {}
    per_file = {}
    for path in sorted(ROOT.rglob("*.py")):
        try:
            src = path.read_text()
        except Exception:
            continue
        rel = path.relative_to(ROOT)
        for lit in STR.findall(src):
            if len(lit) < 8 or lit[0] == '"' and len(lit) < 10:
                continue
            for name, rx in PATTERNS.items():
                if rx.search(lit):
                    counts[name] = counts.get(name, 0) + 1
                    per_file.setdefault(rel, {}).setdefault(name, 0)
                    per_file[rel][name] += 1
    print("TOTALS:")
    for k in sorted(counts, key=lambda k: -counts[k]):
        print(f"  {k:20s} {counts[k]}")
    print("\nPER FILE (files with hits):")
    for f in sorted(per_file, key=lambda f: -sum(per_file[f].values())):
        hits = per_file[f]
        top = ", ".join(f"{k}={v}" for k, v in sorted(hits.items(), key=lambda kv: -kv[1]))
        print(f"  {str(f):55s} {top}")


if __name__ == "__main__":
    main()
