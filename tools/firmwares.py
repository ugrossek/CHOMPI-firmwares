#!/usr/bin/env python3
"""Keep the CHOMPI firmware list up to date.

    firmwares.py poll            look for new releases, record them in releases/
    firmwares.py check           validate firmwares/*.json (for pull requests)
    firmwares.py build [--out]   write the site: catalog.json, the .bin files

Each firmware has an entry in firmwares/<id>.json, written by its developer
and approved once. New versions come from the GitHub releases of the repo the
entry names, or from a version index on the developer's own site (see
index_releases); poll finds them, checks that the file is a CHOMPI firmware
and records its size and checksum in releases/<id>.json. build then copies
the files onto the site, because the browser may not fetch them from there.

Standard library only. Set GITHUB_TOKEN (or have `gh` logged in).
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = ROOT / "firmwares"
RELEASES = ROOT / "releases"
LAUNCHER = ROOT / "launcher"
CACHE = ROOT / ".cache"  # extracted firmware files by sha256, kept between runs

MAX_IMAGE = 0x80000  # the launcher's buffer, chainload.h
APP_START = 0x24000000
KEEP = 4  # releases and pre-releases each that a pull request check tries
CARD_RE = re.compile(r"^[A-Z0-9_-]{1,16}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
REQUIRED = ("title", "card", "author", "summary", "source")
KNOWN = set(REQUIRED) | {"folder", "color", "slot", "stock", "version", "maintainers", "skip", "prefer", "authorUrl", "about"}
SOURCE_KNOWN = {"repo", "index", "tag", "asset", "zip"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?Z?)?$")


# ---- GitHub -----------------------------------------------------------------

def token():
    t = os.environ.get("GITHUB_TOKEN")
    if not t:
        try:
            t = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
        except FileNotFoundError:
            pass
    return t or None


_downloads = {}


def http(url, accept="application/vnd.github+json"):
    if url in _downloads:
        return _downloads[url]
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "chompi-firmwares"})
    t = token()
    if t and url.startswith("https://api.github.com/"):
        req.add_header("Authorization", f"Bearer {t}")
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    if not url.startswith("https://api.github.com/"):
        _downloads[url] = data  # a zip holding several firmwares is fetched once
    return data


def releases_of(repo):
    """Published (not draft) releases, newest first."""
    rel = json.loads(http(f"https://api.github.com/repos/{repo}/releases?per_page=50"))
    rel = [r for r in rel if not r.get("draft")]
    rel.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    return rel


# ---- version index (firmwares not released on GitHub) -----------------------

def index_releases(url):
    """A version index on the developer's site, as GitHub-shaped releases,
    newest first, so everything after this treats both sources alike:

        { "changelog": "https://...",            (optional)
          "versions": [ { "version": "1.0.1", "date": "2026-10-13",
                          "file": "https://.../1.0.1/FRIZZ.bin",
                          "notes": "https://...", (optional)
                          "prerelease": false } ] } (optional)

    The file's ETag / Last-Modified stands in for GitHub's asset id, so a file
    replaced under the same link is noticed. Returns (releases, problems)."""
    data = json.loads(http(url, accept="application/json"))
    rels, bad = [], []
    for i, v in enumerate(data.get("versions", []) if isinstance(data, dict) else []):
        p = index_version_problem(v)
        if p:
            bad.append(f"version {i + 1} in the index: {p}")
            continue
        date = v["date"] if "T" in v["date"] else v["date"] + "T00:00:00Z"
        rels.append({
            "tag_name": v["version"],
            "prerelease": bool(v.get("prerelease")),
            "published_at": date if date.endswith("Z") else date + "Z",
            "html_url": v.get("notes") or data.get("changelog") or url,
            "assets": [{
                "name": v["file"].rsplit("/", 1)[-1],
                "id": v["file"],
                "updated_at": file_stamp(v["file"]),
                "browser_download_url": v["file"],
            }],
        })
    if not isinstance(data, dict) or not isinstance(data.get("versions"), list):
        bad.append("the index has no `versions` list")
    rels.sort(key=lambda r: r["published_at"], reverse=True)
    return rels, bad


def index_version_problem(v):
    if not isinstance(v, dict):
        return "not an object"
    if not isinstance(v.get("version"), str) or not re.match(r"^[\w.+-]{1,32}$", v["version"]):
        return "`version`: 1-32 of letters, digits . + - _"
    if not isinstance(v.get("date"), str) or not DATE_RE.match(v["date"]):
        return "`date`: YYYY-MM-DD"
    if not isinstance(v.get("file"), str) or not v["file"].startswith("https://"):
        return "`file`: an https:// link to the .bin"
    if "notes" in v and not (isinstance(v["notes"], str) and v["notes"].startswith("https://")):
        return "`notes`: an https:// link"
    return None


def file_stamp(url):
    """ETag or Last-Modified of a file, '' if the server gives neither."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "chompi-firmwares"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.headers.get("ETag") or r.headers.get("Last-Modified") or ""
    except urllib.error.URLError:
        return ""


def source_releases(e):
    """(releases, problems) for an entry, from GitHub or its index."""
    s = e["source"]
    if "index" in s:
        return index_releases(s["index"])
    return releases_of(s["repo"]), []


def source_name(e):
    s = e["source"]
    return s.get("repo") or s["index"]


# ---- entries ----------------------------------------------------------------

def load_entries():
    out = {}
    for p in sorted(ENTRIES.glob("*.json")):
        out[p.stem] = json.loads(p.read_text())
    return out


def entry_problems(eid, e, all_entries):
    """Everything wrong with one entry, as sentences. Empty if it is fine."""
    bad = []
    if not ID_RE.match(eid):
        bad.append(f"file name `{eid}.json`: use lower case letters, digits and `-`")
    for k in REQUIRED:
        if k not in e:
            bad.append(f"`{k}` is missing")
    for k in e:
        if k not in KNOWN:
            bad.append(f"unknown field `{k}`")
    if bad:
        return bad
    if not CARD_RE.match(e["card"]):
        bad.append("`card` must be 1-16 of A-Z 0-9 _ - (it becomes the file name on the SD card)")
    for other, o in all_entries.items():
        if other != eid and o.get("card") == e["card"]:
            bad.append(f"`card` {e['card']} is already used by `{other}`")
    if len(e["title"]) > 24:
        bad.append("`title`: 24 characters at most")
    if not 10 <= len(e["summary"]) <= 200:
        bad.append("`summary`: one sentence, 10-200 characters")
    if "color" in e and not re.match(r"^#[0-9A-F]{6}$", str(e["color"])):
        bad.append("`color`: the key's colour as #RRGGBB, capital letters")
    if "folder" in e and not CARD_RE.match(e["folder"]):
        bad.append("`folder`: the folder name on the card, 1-16 of A-Z 0-9 _ -")
    if "slot" in e and not (e.get("stock") and isinstance(e["slot"], int) and 1 <= e["slot"] <= 15):
        bad.append("`slot` is only for stock firmwares (1-15)")
    if e.get("prefer", "stable") not in ("stable", "newest"):
        bad.append("`prefer`: `stable` or `newest`")
    s = e["source"]
    if not isinstance(s, dict):
        return bad + ["`source` must be an object"]
    for k in s:
        if k not in SOURCE_KNOWN:
            bad.append(f"unknown field `source.{k}`")
    if ("repo" in s) == ("index" in s):
        bad.append("`source`: either `repo` (GitHub releases) or `index` (a version index), not both")
    elif "repo" in s and not re.match(r"^[\w.-]+/[\w.-]+$", s["repo"]):
        bad.append("`source.repo`: owner/name of a GitHub repository")
    elif "index" in s:
        if not str(s["index"]).startswith("https://"):
            bad.append("`source.index`: an https:// link to the version index")
        if not str(e.get("authorUrl", "")).startswith("https://"):
            bad.append("`authorUrl` is needed with `source.index` (there is no GitHub owner to link to)")
        if not e.get("maintainers"):
            bad.append("`maintainers` is needed with `source.index`: the GitHub logins that may change the entry")
    for k in ("tag", "asset", "zip"):
        if k in s:
            try:
                re.compile(s[k])
            except re.error as err:
                bad.append(f"`source.{k}` is not a valid regular expression: {err}")
    if "repo" in s and "asset" not in s:
        bad.append("`source.asset` is missing")
    return bad


# ---- images -----------------------------------------------------------------

def image_problem(data):
    """Why this isn't a firmware the launcher would start, or None."""
    if len(data) < 8:
        return "the file is empty"
    if len(data) > MAX_IMAGE:
        return f"{len(data)} bytes, more than the launcher can hold ({MAX_IMAGE})"
    sp = int.from_bytes(data[0:4], "little")
    pc = int.from_bytes(data[4:8], "little")
    if not (0x20000000 <= sp <= 0x20020000 or 0x24000000 <= sp <= 0x24080000):
        return "not a CHOMPI firmware (its vector table doesn't fit the launcher)"
    if not (APP_START <= pc < APP_START + len(data) and pc & 1):
        return "not a CHOMPI firmware (its entry point isn't inside the image)"
    if re.search(rb"CHOMPI-LAUNCHER \d", data):
        return "this is a launcher, not a firmware"
    return None


def extract(blob, member_re):
    """The firmware inside a zip asset."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = [n for n in z.namelist() if re.search(member_re, n)]
    if len(names) != 1:
        raise ValueError(f"the zip has {len(names)} files matching `{member_re}`, expected 1")
    return z.read(names[0])


def fetch_image(rec, member_re=None):
    """Download a recorded version, from the cache if we have it."""
    sha = rec.get("sha256")
    if sha and (CACHE / f"{sha}.bin").exists():
        return (CACHE / f"{sha}.bin").read_bytes()
    blob = http(rec["download"], accept="application/octet-stream")
    data = extract(blob, member_re) if member_re else blob
    CACHE.mkdir(exist_ok=True)
    (CACHE / f"{hashlib.sha256(data).hexdigest()}.bin").write_bytes(data)
    return data


# ---- poll -------------------------------------------------------------------

def version_of(entry, release, asset_name):
    """The version shown on the page: `version` if the entry pins it, else
    the regex group `v` from the asset name or the tag, else the tag."""
    if entry.get("version"):
        return entry["version"]
    s = entry["source"]
    for pattern, text in ((s.get("asset", ""), asset_name), (s.get("tag", ""), release["tag_name"])):
        m = re.search(pattern, text)
        if m and "v" in m.groupdict() and m.group("v"):
            return m.group("v")
    return release["tag_name"]


def candidates(entry, rels):
    """(release, asset) pairs this entry could take, newest first."""
    s = entry["source"]
    skip = set(entry.get("skip", []))
    for r in rels:
        if "tag" in s and not re.search(s["tag"], r["tag_name"]):
            continue
        if r["tag_name"] in skip:
            continue
        assets = [a for a in r.get("assets", []) if re.search(s.get("asset", ""), a["name"])]
        if assets:
            yield r, assets[0]


def kept(entry, rels):
    """The candidates worth remembering: the newest KEEP releases and the
    newest KEEP pre-releases, so a day of betas doesn't push out 0.9."""
    n = {True: 0, False: 0}
    for r, a in candidates(entry, rels):
        if n[r["prerelease"]] < KEEP:
            n[r["prerelease"]] += 1
            yield r, a


def load_releases(eid):
    p = RELEASES / f"{eid}.json"
    return json.loads(p.read_text())["versions"] if p.exists() else []


def save_releases(eid, versions):
    RELEASES.mkdir(exist_ok=True)
    p = RELEASES / f"{eid}.json"
    p.write_text(json.dumps({"versions": versions}, indent=2) + "\n")


def poll_entry(eid, e, rels, write=True):
    """Bring releases/<id>.json up to date. Returns report lines.

    Every version ever recorded stays in the file, so the page recognises it
    on a card and offers the update. One that left GitHub (deleted, skipped,
    or past the newest 50 releases) is kept with `gone`; it is never offered
    for download again."""
    report = []
    old = {v["tag"]: v for v in load_releases(eid)}
    new, used = [], set()
    for r, a in candidates(e, rels):
        known = old.get(r["tag_name"])
        if not known:
            # the firmware moved (GitHub tag v1.0.1 -> index version 1.0.1):
            # the same version is the same record, not a new release
            v = version_of(e, r, a["name"])
            known = next((o for t, o in old.items() if o["version"] == v and t not in used
                          and not any(n["tag"] == t for n in new)), None)
        if known:
            used.add(known["tag"])
        if known and known.get("asset_id") == a["id"] and known.get("asset_updated") == a["updated_at"]:
            known = dict(known, prerelease=r["prerelease"])  # may be flipped later on GitHub
            known.pop("gone", None)
            new.append(known)
            continue
        rec = {
            "tag": r["tag_name"],
            "version": version_of(e, r, a["name"]),
            "prerelease": r["prerelease"],
            "published": r["published_at"],
            "url": r["html_url"],
            "asset": a["name"],
            "asset_id": a["id"],
            "asset_updated": a["updated_at"],
            "download": a["browser_download_url"],
        }
        what = "replaced its file" if known else "new"
        try:
            data = fetch_image(rec, e["source"].get("zip"))
            why = image_problem(data)
        except (ValueError, zipfile.BadZipFile, urllib.error.URLError) as err:
            why = str(err)
        if not why and known and known.get("sha256") == hashlib.sha256(data).hexdigest():
            # same file: a new link or time stamp only. Keep the record (and
            # its publish date), say so only if it moved
            if known.get("download") != rec["download"]:
                report.append(f"- {e['title']} {known['version']}: same file, now from {rec['download']}")
            rec.update(published=known["published"], size=known["size"], sha256=known["sha256"])
            new.append(rec)
            continue
        if why:
            rec["rejected"] = why
            report.append(f"- **{e['title']} {rec['version']}** ({what}): not taken, {why}. [release]({rec['url']})")
        else:
            rec["size"] = len(data)
            rec["sha256"] = hashlib.sha256(data).hexdigest()
            pre = " (pre-release)" if r["prerelease"] else ""
            report.append(f"- {e['title']} {rec['version']}{pre}: {what}, {len(data) // 1024} KB. [release]({rec['url']})")
        new.append(rec)
    seen = {v["tag"] for v in new} | used
    for t, v in old.items():
        if t in seen:
            continue
        if not v.get("gone"):
            v = dict(v, gone=True)
            report.append(f"- {e['title']} {v['version']}: no longer released (or skipped); "
                          "not offered any more, still recognised on cards")
        new.append(v)
    new.sort(key=lambda v: v["published"], reverse=True)
    if write and new != list(old.values()):
        save_releases(eid, new)
    return report


def cmd_poll(args):
    entries = load_entries()
    by_repo, report, failed = {}, [], []
    for eid, e in entries.items():
        if args.only and eid not in args.only:
            continue
        if entry_problems(eid, e, entries):
            failed.append(f"- `{eid}`: entry is invalid, skipped (run `check`)")
            continue
        name = source_name(e)
        try:
            if name not in by_repo:
                by_repo[name] = source_releases(e)
            rels, bad = by_repo[name]
        except (urllib.error.URLError, ValueError) as err:
            failed.append(f"- {e['title']}: couldn't read the releases of {name}: {err}")
            continue
        failed += [f"- {e['title']}: {b}" for b in bad]
        if not rels and bad:
            continue  # a broken index must not mark every version gone
        report += poll_entry(eid, e, rels)
    text = "\n".join(report + failed)
    if args.report:
        Path(args.report).write_text(text + ("\n" if text else ""))
    print(text or "nothing new")


# ---- check (pull requests) --------------------------------------------------

def cmd_check(args):
    entries = load_entries()
    out, errors = [], 0
    pr_author = (args.author or "").lower()
    changed = set(args.changed or entries)
    # Only firmwares/<id>.json is read: anything else there would be skipped without a word.
    for p in sorted(ENTRIES.iterdir()):
        if p.suffix != ".json":
            errors += 1
            out.append(f"### `firmwares/{p.name}`")
            out.append(f"- :x: not read: an entry must be `firmwares/<id>.json`")
    for eid in sorted(changed):
        if eid not in entries:
            out.append(f"### `{eid}`: removed")
            continue
        e = entries[eid]
        bad = entry_problems(eid, e, entries)
        out.append(f"### {e.get('title', eid)} (`firmwares/{eid}.json`)")
        if bad:
            errors += len(bad)
            out += [f"- :x: {b}" for b in bad]
            continue
        repo = source_name(e)
        owner = e["source"]["repo"].split("/")[0].lower() if "repo" in e["source"] else None
        maint = [m.lower() for m in e.get("maintainers", [])]
        if pr_author:
            if pr_author == owner or pr_author in maint:
                out.append(f"- :white_check_mark: opened by @{args.author}, who owns `{repo}` or maintains this entry")
            else:
                out.append(f"- :warning: opened by @{args.author}, who neither owns `{repo}` nor is listed in `maintainers`")
        try:
            rels, bad = source_releases(e)
        except (urllib.error.URLError, ValueError) as err:
            errors += 1
            out.append(f"- :x: can't read the releases of `{repo}`: {err}")
            continue
        errors += len(bad)
        out += [f"- :x: {b}" for b in bad]
        found = list(kept(e, rels))
        if not found:
            errors += 1
            out.append(f"- :x: no release of `{repo}` matches `source` (tag `{e['source'].get('tag', 'any')}`, asset `{e['source'].get('asset', 'any')}`)")
            continue
        out.append("- releases it would take:")
        for r, a in found:
            rec = {"download": a["browser_download_url"]}
            try:
                why = image_problem(fetch_image(rec, e["source"].get("zip")))
            except (ValueError, zipfile.BadZipFile, urllib.error.URLError) as err:
                why = str(err)
            pre = ", pre-release" if r["prerelease"] else ""
            v = version_of(e, r, a["name"])
            out.append(f"  - {v} (`{r['tag_name']}`, `{a['name']}`{pre}): " + (f":x: {why}" if why else ":white_check_mark: CHOMPI firmware"))
        others = [o for oid, o in entries.items() if oid != eid and e.get("folder") and o.get("folder") == e["folder"]]
        if others:
            out.append(f"- note: shares the folder `{e['folder']}` with " + ", ".join(o["title"] for o in others))
    text = "\n".join(out)
    if args.report:
        Path(args.report).write_text(text + "\n")
    print(text)
    sys.exit(1 if errors else 0)


# ---- build ------------------------------------------------------------------

def stage_of(e, rec):
    if e.get("stock"):
        return "stock"
    v = rec["version"].lower()
    if "alpha" in v:
        return "alpha"
    if rec["prerelease"] or "beta" in v:
        return "beta"
    return "release"


def safe(s):
    return re.sub(r"[^A-Za-z0-9._-]", "-", s)


def cmd_build(args):
    out = Path(args.out)
    entries = load_entries()
    firmwares, problems = [], []
    for eid, e in entries.items():
        if entry_problems(eid, e, entries):
            problems.append(f"{eid}: invalid entry, left out")
            continue
        known = [r for r in load_releases(eid) if "sha256" in r]
        recs = [r for r in known if not r.get("gone")]
        if not recs:
            problems.append(f"{eid}: no usable release yet, left out")
            continue
        stable = [r for r in recs if not r["prerelease"]]
        order = stable + [r for r in recs if r["prerelease"]] if e.get("prefer", "stable") == "stable" and stable else recs
        cur = data = None
        for r in order:  # the first one that still downloads and matches
            try:
                data = fetch_image(r, e["source"].get("zip"))
            except (ValueError, zipfile.BadZipFile, urllib.error.URLError) as err:
                problems.append(f"{eid} {r['version']}: {err}")
                continue
            if hashlib.sha256(data).hexdigest() == r["sha256"]:
                cur = r
                break
            problems.append(f"{eid} {r['version']}: file changed since poll, skipped")
        if not cur:
            problems.append(f"{eid}: no version could be fetched, left out")
            continue
        rel = f"bin/{eid}/{safe(e['card'])}-{safe(cur['version'])}.bin"
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(data)
        owner = e["source"].get("repo", "").split("/")[0]
        f = {
            "id": eid,
            "title": e["title"],
            "card": e["card"],
            "version": cur["version"],
            "stage": stage_of(e, cur),
            "author": e["author"],
            "authorUrl": e.get("authorUrl", f"https://github.com/{owner}"),
            "summary": e["summary"],
            "url": cur["url"],
            "released": cur["published"][:10],
            "file": rel,
            "size": cur["size"],
            "sha256": cur["sha256"],
            "checked": bool(e.get("maintainers") or e.get("stock")),
        }
        for k in ("folder", "slot", "color"):
            if k in e:
                f[k] = e[k]
        prev = [{"version": r["version"], "size": r["size"], "sha256": r["sha256"]}
                for r in known if r["published"] < cur["published"] and r["sha256"] != cur["sha256"]]
        if prev:
            f["previous"] = prev
        firmwares.append(f)
    # stock in key order, then the most recently released first
    firmwares.sort(key=lambda f: f["released"], reverse=True)
    firmwares.sort(key=lambda f: (f["stage"] != "stock", f.get("slot", 99)))
    catalog = {
        "about": "Firmwares the CHOMPI setup page offers. Built from firmwares/*.json and their releases "
                 "by tools/firmwares.py. 'checked': the author maintains the entry. 'previous': earlier versions, "
                 "so the page recognises them on a card.",
        "firmwares": firmwares,
    }
    lj = LAUNCHER / "launcher.json"
    if lj.exists():
        info = json.loads(lj.read_text())
        data = (LAUNCHER / "CHOMPI.bin").read_bytes()
        if f"CHOMPI-LAUNCHER {info['version']}".encode() not in data:
            sys.exit(f"launcher/CHOMPI.bin doesn't carry the tag CHOMPI-LAUNCHER {info['version']}")
        (out / "launcher").mkdir(parents=True, exist_ok=True)
        (out / "launcher/CHOMPI.bin").write_bytes(data)
        catalog["launcher"] = {"version": info["version"], "file": "launcher/CHOMPI.bin",
                               "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    (out / "catalog.json").write_text(json.dumps(catalog, indent=2) + "\n")
    site = ROOT / "site"
    if site.is_dir():
        for p in site.rglob("*"):
            if p.is_file():
                (out / p.relative_to(site)).parent.mkdir(parents=True, exist_ok=True)
                (out / p.relative_to(site)).write_bytes(p.read_bytes())
    print(f"{len(firmwares)} firmwares -> {out}/catalog.json")
    for p in problems:
        print("  " + p)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll")
    p.add_argument("--only", nargs="*", help="entry ids")
    p.add_argument("--report", help="write the markdown report here")
    p.set_defaults(fn=cmd_poll)
    p = sub.add_parser("check")
    p.add_argument("--changed", nargs="*", help="entry ids the PR touches (default: all)")
    p.add_argument("--author", help="GitHub login that opened the PR")
    p.add_argument("--report", help="write the markdown report here")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("build")
    p.add_argument("--out", default="_site")
    p.set_defaults(fn=cmd_build)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
