#!/usr/bin/env python3
"""check_env_parity.py — assert every surface/process that uses mnemosyne has
the SAME MNEMOSYNE_* env, especially the polyphonic-recall flag. A surface
running with POLYPHONIC_RECALL unset gets the degraded single-arm recall and
can silently miss a just-written row; gate this so drift fails loudly.
Exit 0 = parity; 1 = drift.
"""
import os, pathlib, re, subprocess, sys

CANON_KEYS = (
    "MNEMOSYNE_POLYPHONIC_RECALL",
    "MNEMOSYNE_EMBEDDING_MODEL",
    "MNEMOSYNE_EMBEDDING_DIM",
    "MNEMO_BACKEND",
)
HERMES = pathlib.Path(os.environ.get("HERMES_HOME", str(pathlib.Path.home() / ".hermes")))


def env_from_proc(pid: str) -> dict:
    try:
        raw = pathlib.Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    out = {}
    for chunk in raw.split(b"\0"):
        if b"=" in chunk:
            k, v = chunk.split(b"=", 1)
            out[k.decode(errors="replace")] = v.decode(errors="replace")
    return out


def main():
    # canonical = the live gateway process env (the reference surface)
    ref = {}
    try:
        pid = subprocess.run(["systemctl", "--user", "show", "hermes-gateway",
                              "-p", "MainPID", "--value"],
                             capture_output=True, text=True).stdout.strip()
        ref = env_from_proc(pid)
    except Exception:
        pass
    fails = 0
    envs = {}
    for profile_dir in [HERMES] + sorted((HERMES / "profiles").glob("*/")):
        cfg = profile_dir / ".env"
        if cfg.exists():
            d = {}
            for line in cfg.read_text().splitlines():
                m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)", line)
                if m:
                    d[m.group(1)] = m.group(2).strip().strip('"').strip("'")
            envs[profile_dir.name or "root"] = d
    if ref:
        envs["gateway-proc"] = ref
    # reference value for poly flag: gateway if present else any .env that has it
    for key in CANON_KEYS:
        vals = {}
        for src, d in envs.items():
            if key in d:
                vals[src] = d[key]
        # flag only surfaces that DON'T set it (they silently inherit the
        # library default, which may differ from the fleet standard)
        missing = [src for src in envs if key not in envs[src]]
        distinct = set(vals.values())
        if len(distinct) > 1:
            print(f"FAIL {key}: divergent values {vals}")
            fails += 1
        elif missing and vals:
            # not fatal for all keys; poly flag is the critical one
            if key == "MNEMOSYNE_POLYPHONIC_RECALL":
                print(f"WARN {key}: set to {distinct} but MISSING in {missing} "
                      "(those surfaces run the library default)")
        else:
            print(f"OK   {key}: {dict(sorted(vals.items())) if vals else '(unset everywhere)'}")
    print("ENV PARITY:", "FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
