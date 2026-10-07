#!/usr/bin/env python3
"""Copy existing LaTeX into semantic fragments while preserving TeX output.

prepare freezes source bytes and proposes safe structural boundaries. A naming
agent writes only {id, slug} pairs to plan.json. apply validates that plan and
copies frozen byte slices. Marked assembly guards preserve legacy input EOF
space tokens. It does not compile or modify delivery PDFs; compilation and
page comparison are an additional required gate outside this tool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / ".tactus/scripts/Support/volumes.json"
INPUT = re.compile(rb"\\(?:input|include)\s*\{(chapters/[^{}]+)\}")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
COMMAND = re.compile(rb"\\([A-Za-z@]+|[^A-Za-z@])")
HEADING_ARITY = {
    "chapter": 1, "section": 1, "subsection": 1, "part": 1,
    "BourbakiStarChapter": 1, "BourbakiStarSection": 1,
    "BourbakiUnnumberedChapter": 2, "BourbakiFrontChapter": 1,
    "BourbakiFrontMatter": 1, "BourbakiBackMatter": 1,
    "BourbakiBackSection": 1, "BourbakiAppendixSection": 1,
    "BourbakiExercisesHeading": 0, "BourbakiExercisesSection": 0,
    "BourbakiExercises": -1, "BourbakiAppendixExercises": -1,
    "BourbakiExerciseGroup": 0,
    # The completed books define several non-Bourbaki aliases for actual TOC
    # levels. Their arities were checked against every volume's preamble.
    "specialchapter": 1, "specialsection": 1, "specsection": 1,
    "exercisesection": 1, "startexercises": 0, "exercisegroup": 0,
    "appendixexercisegroup": 0, "ExerciseSection": 0, "ExerciseAppendix": 0,
    "frontmatterentry": 1, "backmatterentry": 1, "exercisesheading": 0,
    "exerciseappendix": 0, "chapterappendix": 1, "appendixheading": 0,
    "annexsubsection": 1, "exercisesfor": 1, "exercisesforappendix": 0,
    "appendixsection": 1, "hnsection": 1, "summarychapter": 3,
    "appendixitem": 1, "plainsection": 1, "histitem": 1, "exerciseshdr": 1,
}
STATE_COMMANDS = {"frontmatter", "mainmatter", "backmatter", "appendix",
                  "BourbakiMainMatter", "setcounter", "addtocounter",
                  "ExerciseGroupsStart", "ResumeMainChapters"}
ALIASES = {
    "specialchapter": "BourbakiStarChapter", "summarychapter": "BourbakiStarChapter",
    "frontmatterentry": "BourbakiFrontMatter", "backmatterentry": "BourbakiBackMatter",
    "specialsection": "BourbakiStarSection", "specsection": "BourbakiStarSection",
    "plainsection": "BourbakiStarSection", "hnsection": "BourbakiStarSection",
    "histitem": "BourbakiStarSection", "exercisesection": "BourbakiExercisesSection",
    "startexercises": "BourbakiExercisesHeading", "exercisesheading": "BourbakiExercisesHeading",
    "exerciseshdr": "BourbakiExercisesHeading", "exercisegroup": "BourbakiExerciseGroup",
    "ExerciseSection": "BourbakiExerciseGroup", "exercisesfor": "BourbakiExerciseGroup",
    "appendixexercisegroup": "BourbakiAppendixExercises", "ExerciseAppendix": "BourbakiAppendixExercises",
    "exerciseappendix": "BourbakiAppendixExercises", "exercisesforappendix": "BourbakiAppendixExercises",
    "chapterappendix": "BourbakiAppendixSection", "appendixheading": "BourbakiAppendixSection",
    "appendixsection": "BourbakiAppendixSection", "annexsubsection": "subsection",
    "appendixitem": "subsection",
}
CONDITIONALS = {"if", "ifcat", "ifnum", "ifdim", "ifodd", "ifvmode", "ifhmode", "ifmmode",
                "ifinner", "ifvoid", "ifhbox", "ifvbox", "ifx", "ifeof", "iftrue", "iffalse",
                "ifcase", "ifdefined", "ifcsname", "iffontchar"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_volume(order: int):
    return next(v for v in load_json(CATALOG)["volumes"] if v["order"] == order)


def task_dir(volume):
    return ROOT / "work/semantic_fragments" / volume["job"]


def source_dir(volume, language):
    return ROOT / ("ConvertedLatex(EN)" if language == "en" else "ConvertedLatex(CH)") / volume["volume"]


def balanced(data: bytes, pos: int, opener=123, closer=125):
    """Return the exact argument and end position, respecting TeX escapes/comments."""
    if pos >= len(data) or data[pos] != opener:
        raise ValueError(f"Expected argument at byte {pos}")
    depth, i = 1, pos + 1
    while i < len(data):
        if data[i] == 92:
            i += 2
            continue
        if data[i] == 37:
            end = data.find(b"\n", i)
            i = len(data) if end < 0 else end + 1
            continue
        if data[i] == opener:
            depth += 1
        elif data[i] == closer:
            depth -= 1
            if not depth:
                return data[pos + 1:i], i + 1
        i += 1
    raise ValueError(f"Unclosed argument at byte {pos}")


def skip_space(data, pos):
    while pos < len(data) and data[pos] in b" \t\r\n":
        pos += 1
    return pos


def read_arguments(data, pos, count):
    args = []
    for _ in range(count):
        pos = skip_space(data, pos)
        arg, pos = balanced(data, pos)
        args.append(arg.decode("utf-8"))
    return args, pos


def scan(data: bytes):
    """Scan only line-start commands at brace/environment/math depth zero.

    Existing malformed structures are reported, never repaired. Unsafe headings
    are retained inside their enclosing fragment rather than used as cuts.
    """
    events, warnings, envs, conditionals = [], [], [], []
    depth, math, i = 0, None, 0
    while i < len(data):
        b = data[i]
        if b == 37:
            end = data.find(b"\n", i)
            i = len(data) if end < 0 else end + 1
            continue
        if b == 92:
            start = i
            match = COMMAND.match(data, i)
            if not match:
                i += 1
                continue
            command = match[1].decode("ascii", errors="replace")
            i = match.end()
            line_start = data.rfind(b"\n", 0, start) + 1
            at_start = not data[line_start:start].strip(b" \t\r")
            safe = depth == 0 and not envs and math is None and not conditionals
            if command in CONDITIONALS and depth == 0:
                conditionals.append(command)
                continue
            if command == "fi" and depth == 0 and conditionals:
                conditionals.pop()
                continue
            if command in HEADING_ARITY and at_start:
                starred = i < len(data) and data[i:i+1] == b"*"
                arg_start = i + int(starred)
                arg_start = skip_space(data, arg_start)
                if arg_start < len(data) and data[arg_start:arg_start+1] == b"[":
                    _, arg_start = balanced(data, arg_start, 91, 93)
                arity = HEADING_ARITY[command]
                if arity < 0:
                    arity = int(data[skip_space(data, arg_start):skip_space(data, arg_start)+1] == b"{")
                args, end = read_arguments(data, arg_start, arity)
                event = {"offset": line_start, "command_offset": start, "end": end,
                         "command": command, "starred": starred,
                         "title": args[1] if command == "summarychapter" else args[0] if args else command.replace("Bourbaki", ""),
                         "arguments": args}
                if safe:
                    events.append(event)
                else:
                    warnings.append({"offset": start, "command": command,
                                     "reason": "heading inside an existing brace, environment, or math",
                                     "depth": depth, "environments": list(envs), "math": math,
                                     "conditionals": list(conditionals),
                                     "action": "keep_inside_enclosing_fragment"})
                i = end
                continue
            if command in STATE_COMMANDS and safe and at_start:
                args, end = read_arguments(data, i, 2 if command in {"setcounter", "addtocounter"} else 0)
                events.append({"offset": line_start, "command_offset": start, "end": end,
                               "command": command, "arguments": args, "state_only": True})
                i = end
                continue
            if command in {"begin", "end"}:
                pos = skip_space(data, i)
                if data[pos:pos+1] == b"{":
                    env, i = balanced(data, pos)
                    name = env.decode("utf-8")
                    if command == "begin":
                        envs.append(name)
                    elif envs and envs[-1] == name:
                        envs.pop()
                    else:
                        warnings.append({"offset": start, "reason": "existing unmatched environment end", "environment": name})
                    continue
            if command in {"(", "["}:
                math = command
            elif command in {")", "]"}:
                math = None
            elif command in {"verb", "verb*"} and i < len(data):
                delimiter = data[i:i+1]
                end = data.find(delimiter, i + 1)
                i = len(data) if end < 0 else end + 1
            continue
        if b == 123:
            depth += 1
        elif b == 125:
            depth -= 1
            if depth < 0:
                warnings.append({"offset": i, "reason": "existing unmatched closing brace"})
                depth = 0
        elif b == 36:
            if data[i:i+2] == b"$$":
                math = None if math == "$$" else "$$"
                i += 1
            else:
                math = None if math == "$" else "$"
        i += 1
    if depth or envs or math or conditionals:
        warnings.append({"offset": len(data), "reason": "existing unclosed lexical structure",
                         "depth": depth, "environments": envs, "math": math, "conditionals": conditionals})
    return events, warnings


class Hierarchy:
    def __init__(self):
        self.counts = {"chapter": 0, "section": 0, "subsection": 0}
        self.mode, self.chapter, self.section, self.subsection = "front", None, None, None
        self.chapter_title = self.section_title = self.subsection_title = None
        self.serial, self.appendix, self.exercise = {}, 0, 0

    def fresh(self, prefix):
        self.serial[prefix] = self.serial.get(prefix, 0) + 1
        return f"{prefix}{self.serial[prefix]:02d}"

    def state(self, event):
        cmd, args = event["command"], event.get("arguments", [])
        if cmd in {"frontmatter", "mainmatter", "backmatter", "BourbakiMainMatter"}:
            self.mode = {"frontmatter": "front", "mainmatter": "main", "backmatter": "back",
                         "BourbakiMainMatter": "main"}[cmd]
        elif cmd == "appendix":
            self.mode = "appendix"
            self.counts["chapter"] = 0
            self.counts["section"] = 0
        elif cmd == "ExerciseGroupsStart":
            self.exercise = 0
        elif cmd == "ResumeMainChapters":
            self.mode = "main"
            self.counts["chapter"] = 9
        elif cmd in {"setcounter", "addtocounter"} and args[0] in self.counts and re.fullmatch(r"-?\d+", args[1]):
            self.counts[args[0]] = int(args[1]) + (self.counts[args[0]] if cmd == "addtocounter" else 0)

    def heading(self, event):
        original_cmd = event["command"]
        cmd, star, title = ALIASES.get(original_cmd, original_cmd), event.get("starred", False), event["title"]
        if ((cmd == "chapter" and star) or cmd == "BourbakiStarChapter" or (cmd == "section" and star)) and re.fullmatch(r"\s*(?:exercises|习题|练习)\s*", title, re.I):
            cmd = "BourbakiExercisesHeading"
        kind, level = "", 0
        if cmd in {"chapter", "BourbakiStarChapter", "BourbakiUnnumberedChapter", "BourbakiFrontChapter",
                   "BourbakiFrontMatter", "BourbakiBackMatter", "part"}:
            self.counts["section"] = self.counts["subsection"] = 0
            self.section = self.subsection = None
            self.section_title = self.subsection_title = None
            self.appendix = self.exercise = 0
            if cmd == "chapter" and not star:
                self.counts["chapter"] += 1
                prefix = "app" if self.mode == "appendix" else "ch"
                ident = f"{prefix}{self.counts['chapter']:02d}"
                kind = "appendix_intro" if prefix == "app" else "chapter_intro"
            else:
                mode = "front" if cmd in {"BourbakiFrontChapter", "BourbakiFrontMatter"} else self.mode
                if cmd == "BourbakiBackMatter":
                    mode = self.mode = "back"
                if cmd == "part":
                    mode = "part"
                if mode == "main":
                    mode = "unnumbered"
                ident = self.fresh(mode)
                kind = mode + "_intro"
            self.chapter, self.chapter_title = ident, title
            level, parent = 1, None
        elif cmd == "BourbakiAppendixSection":
            self.appendix += 1
            self.counts["section"] = self.counts["subsection"] = 0
            self.section = f"{self.chapter or 'book'}-app{self.appendix:02d}"
            ident, self.section_title, self.subsection = self.section, title, None
            kind, level, parent = "appendix_intro", 2, self.chapter
        elif cmd in {"BourbakiExercisesHeading", "BourbakiExercises"}:
            self.exercise = 0
            ident = self.fresh(f"{self.chapter or 'book'}-ex")
            kind, level, parent = "exercises_intro", 2, self.chapter
            self.section, self.section_title, self.subsection = ident, title, None
        elif cmd in {"BourbakiExercisesSection", "BourbakiExerciseGroup", "BourbakiAppendixExercises"}:
            if original_cmd == "exercisesfor" and event["arguments"] and event["arguments"][0].isdigit():
                self.exercise = int(event["arguments"][0])
            else:
                self.exercise += 1
            ident = f"{self.chapter or 'book'}-ex-s{self.exercise:02d}"
            kind = "appendix_exercises" if cmd == "BourbakiAppendixExercises" else "exercises"
            level, parent = 2, self.chapter
            self.section, self.section_title, self.subsection = ident, title, None
        elif cmd in {"section", "BourbakiStarSection", "BourbakiBackSection"}:
            self.counts["subsection"] = 0
            self.subsection = self.subsection_title = None
            if cmd == "section" and not star:
                self.counts["section"] += 1
                ident = f"{self.chapter or 'book'}-s{self.counts['section']:02d}"
                kind = "section_intro"
            else:
                ident = self.fresh(f"{self.chapter or 'book'}-u")
                kind = "back_section" if cmd == "BourbakiBackSection" else "unnumbered_section"
            self.section, self.section_title = ident, title
            level, parent = 2, self.chapter
        elif cmd == "subsection":
            self.counts["subsection"] += 1
            ident = f"{self.section or self.chapter or 'book'}-ss{self.counts['subsection']:02d}"
            self.subsection, self.subsection_title = ident, title
            kind, level, parent = "subsection", 3, self.section or self.chapter
        else:
            raise ValueError(cmd)
        return {"id": ident, "kind": kind, "level": level, "parent_id": parent,
                "context": {"chapter": self.chapter_title, "section": self.section_title,
                            "subsection": self.subsection_title}}


def eof_comment(data):
    """Whether the last physical source line contains an unescaped comment."""
    line = (data[:-1] if data.endswith(b"\n") else data).rsplit(b"\n", 1)[-1]
    for match in re.finditer(rb"%", line):
        before, backslashes = match.start() - 1, 0
        while before >= 0 and line[before] == 92:
            backslashes += 1
            before -= 1
        if backslashes % 2 == 0:
            return True
    return False


def seam_guard(previous, separator):
    """Restore the parent's independent scanner state at a legacy input seam.

    The child EOF and parent input-line end each emit their own space token.
    Plain joining either creates a paragraph or loses one space glue. A no-op
    relax plus an empty group sets the joined line to the parent's original
    mid-line state while retaining its space factor. A bare control word would
    consume the following line-end space. The comment labels assembly syntax.
    """
    if not separator:
        return b""
    return (b"" if previous.endswith(b"\n") else b"\n") + b"% semantic-fragments: legacy-input-seam\n\\relax{}"


def load_source(book: Path):
    main = (book / "main.tex").read_bytes()
    matches = list(INPUT.finditer(main))
    if not matches:
        raise ValueError(f"No chapter input commands in {book}")
    files, groups, group, adjustments = {}, [], None, []
    def expand_file(path, stack=()):
        if path in stack:
            raise ValueError(f"Cyclic chapter inputs: {path}")
        raw = (book / path).read_bytes()
        files[path.as_posix()] = raw
        nested = list(INPUT.finditer(raw))
        if not nested:
            return raw, [{"path": path.as_posix(), "start": 0, "end": len(raw),
                          "group_start": 0, "group_end": len(raw)}], raw
        expanded, raw_expanded, spans, cursor, previous = b"", b"", [], 0, None
        for match in nested:
            separator = raw[cursor:match.start()]
            guard = seam_guard(previous, separator) if previous is not None else b""
            if guard:
                start = len(expanded)
                expanded += guard
                spans.append({"path": "__generated_input_seam__", "start": 0, "end": len(guard),
                              "group_start": start, "group_end": len(expanded)})
            text = separator
            raw_expanded += separator
            start = len(expanded)
            expanded += text
            if text:
                spans.append({"path": path.as_posix(), "start": cursor, "end": match.start(),
                              "group_start": start, "group_end": len(expanded)})
            name = match[1].decode("utf-8")
            child = Path(name if name.endswith(".tex") else name + ".tex")
            if child.is_absolute() or ".." in child.parts:
                raise ValueError(f"Unsafe nested input path: {name}")
            data, child_spans, raw_child = expand_file(child, (*stack, path))
            raw_expanded += raw_child
            start = len(expanded)
            expanded += data
            spans.extend({**s, "group_start": s["group_start"] + start,
                          "group_end": s["group_end"] + start} for s in child_spans)
            cursor = match.end()
            previous = data
        suffix = raw[cursor:]
        guard = seam_guard(previous, suffix) if previous is not None else b""
        if guard:
            start = len(expanded)
            expanded += guard
            spans.append({"path": "__generated_input_seam__", "start": 0, "end": len(guard),
                          "group_start": start, "group_end": len(expanded)})
        text, start = suffix, len(expanded)
        raw_expanded += suffix
        expanded += text
        if text:
            spans.append({"path": path.as_posix(), "start": cursor, "end": len(raw),
                          "group_start": start, "group_end": len(expanded)})
        return expanded, spans, raw_expanded
    for match in matches:
        name = match[1].decode("utf-8")
        path = Path(name if name.endswith(".tex") else name + ".tex")
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe source path: {name}")
        data, spans, raw_data = expand_file(path)
        include = match.group().startswith(b"\\include")
        gap = main[group["main_end"]:match.start()] if group else b""
        # Comment EOF scanner state cannot be reproduced by plain text joining;
        # preserve that source input boundary exactly instead of guessing.
        preserve_eof = group and (eof_comment(group["last_data"]) or
                                  (not group["last_data"].endswith(b"\n") and b"\n" not in gap))
        if group is None or include or group.get("include") or gap.strip() or preserve_eof:
            group = {"main_start": match.start(), "main_end": match.end(), "data": b"", "raw_data": b"", "spans": [],
                     "include": include, "wrapper_path": path.as_posix() if include else None}
            groups.append(group)
        else:
            whitespace = gap
            group["raw_data"] += gap
            guard = seam_guard(group["last_data"], gap)
            if guard:
                start = len(group["data"])
                group["data"] += guard
                group["spans"].append({"path": "__generated_input_seam__", "start": 0, "end": len(guard),
                                       "group_start": start, "group_end": len(group["data"])})
                adjustments.append({"path": "main.tex", "start_byte": group["main_end"],
                                    "end_byte": match.start(), "guard": guard.decode("ascii"),
                                    "reason": "Preserve independent child EOF and parent input-line scanner states"})
            start = len(group["data"])
            group["data"] += whitespace
            if whitespace:
                group["spans"].append({"path": "main.tex", "start": group["main_end"], "end": match.start(),
                                       "group_start": start, "group_end": len(group["data"])})
        start = len(group["data"])
        group["data"] += data
        group["raw_data"] += raw_data
        group["last_data"] = data
        group["spans"].extend({**s, "group_start": s["group_start"] + start,
                                "group_end": s["group_end"] + start} for s in spans)
        group["main_end"] = match.end()
    expanded, raw_expanded, cursor = b"", b"", 0
    for group in groups:
        expanded += main[cursor:group["main_start"]] + group["data"]
        raw_expanded += main[cursor:group["main_start"]] + group["raw_data"]
        cursor = group["main_end"]
    expanded += main[cursor:]
    raw_expanded += main[cursor:]
    return {"main": main, "files": files, "groups": groups, "expanded": expanded,
            "raw_expanded": raw_expanded, "input_seam_adjustments": adjustments,
            "pdf_hash": sha((book / "main.pdf").read_bytes()) if (book / "main.pdf").exists() else None}


def clip_spans(group, start, end):
    result = []
    for span in group["spans"]:
        a, b = max(start, span["group_start"]), min(end, span["group_end"])
        if a >= b:
            continue
        result.append({"path": span["path"], "start_byte": span["start"] + a - span["group_start"],
                       "end_byte": span["start"] + b - span["group_start"],
                       "fragment_start_byte": a - start, "fragment_end_byte": b - start})
    return result


def structures(source):
    hierarchy, pieces, warnings, cursor = Hierarchy(), [], [], 0
    for group_index, group in enumerate(source["groups"]):
        # Only the document body contributes counter and matter state, not macro definitions.
        prefix = source["main"][cursor:group["main_start"]]
        if b"\\begin{document}" in prefix:
            prefix = prefix.split(b"\\begin{document}", 1)[1]
        states, _ = scan(prefix)
        for event in states:
            if event.get("state_only"):
                hierarchy.state(event)
        cursor = group["main_end"]
        events, group_warnings = scan(group["data"])
        warnings.extend({"group": group_index, **w} for w in group_warnings)
        headings = []
        for event in events:
            if event.get("state_only"):
                hierarchy.state(event)
            else:
                headings.append({**event, **hierarchy.heading(event)})
        if not headings or headings[0]["offset"] > 0:
            # Keep all leading bytes, including matter/counter commands, in a standalone intro.
            intro = {"offset": 0, "command": "source-prefix", "starred": False,
                     "title": "Introduction", "id": hierarchy.fresh(f"g{group_index+1:02d}-intro"),
                     "kind": "source_intro", "level": 0, "parent_id": None,
                     "context": {"chapter": None, "section": None, "subsection": None}}
            headings.insert(0, intro)
        for index, heading in enumerate(headings):
            start, end = heading["offset"], headings[index+1]["offset"] if index+1 < len(headings) else len(group["data"])
            if start == end:
                continue
            spans = clip_spans(group, start, end)
            source_spans = [span for span in spans if span["path"] != "__generated_input_seam__"]
            assembly_spans = [span for span in spans if span["path"] == "__generated_input_seam__"]
            for span in source_spans:
                raw = source["main"] if span["path"] == "main.tex" else source["files"][span["path"]]
                span["start_line"] = raw.count(b"\n", 0, span["start_byte"]) + 1
                span["end_line"] = raw.count(b"\n", 0, max(span["start_byte"], span["end_byte"]-1)) + 1
            pieces.append({**heading, "group": group_index, "start": start, "end": end,
                           "bytes": end-start, "sha256": sha(group["data"][start:end]),
                           "source_spans": source_spans, "assembly_spans": assembly_spans})
    # Repeated chapter counters or unusual starred headings must not overwrite files.
    seen = set()
    for piece in pieces:
        original = piece["id"]
        serial = 2
        while piece["id"] in seen:
            piece["id"] = f"{original}-r{serial:02d}"
            serial += 1
        seen.add(piece["id"])
    return pieces, warnings


def suggested_slug(title):
    plain = re.sub(r"\\[A-Za-z]+", " ", title.lower())
    words = re.findall(r"[a-z0-9]+", plain)
    words = [w for w in words if w not in {"the", "of", "a", "an", "and", "in", "to", "for", "on", "with", "text", "mathsf", "mathbf"}]
    if not words:
        return "intro"
    slug = ""
    for word in words:
        proposal = word if not slug else slug + "-" + word
        if len(proposal) > 20:
            break
        slug = proposal
    return slug or words[0][:20].rstrip("-") or "intro"


def prepare(order):
    volume, languages = get_volume(order), {}
    directory = task_dir(volume)
    previous = load_json(directory / "plan-input.json") if (directory / "plan-input.json").exists() else None
    if (directory / "applied.json").exists():
        return verify(order)
    for language in ("en", "zh"):
        book = source_dir(volume, language)
        source = load_source(book)
        if previous:
            frozen_hash = previous["source_hashes"][language]
            if sha(source["main"]) != frozen_hash["main_sha256"] or source["pdf_hash"] != frozen_hash["pdf_sha256"] or {
                name: sha(data) for name, data in source["files"].items()} != frozen_hash["files"]:
                raise ValueError(f"Original source file bytes or delivery PDF changed since first prepare: {language}")
        pieces, warnings = structures(source)
        languages[language] = {"source": source, "pieces": pieces, "warnings": warnings}
    en, zh = languages["en"]["pieces"], languages["zh"]["pieces"]
    signature = lambda p: (p["id"], p["command"], p["starred"])
    if [signature(p) for p in en] != [signature(p) for p in zh]:
        write_json(directory / "structure-mismatch.json", {language: languages[language]["pieces"] for language in languages})
        raise ValueError(f"Bilingual structural boundaries differ; inspect {directory / 'structure-mismatch.json'}")
    entries = []
    for a, b in zip(en, zh):
        entries.append({key: a[key] for key in ("id", "kind", "level", "parent_id", "command", "starred")} |
                       {"context": {"en": a["context"], "zh": b["context"]},
                        "title_en": a["title"], "title_zh": b["title"],
                        "suggested_slug": suggested_slug(a["title"]),
                        "byte_counts": {"en": a["bytes"], "zh": b["bytes"]},
                        "source_spans": {"en": a["source_spans"], "zh": b["source_spans"]}})
    hashes = {}
    for language, value in languages.items():
        source = value["source"]
        frozen = directory / "original" / language
        expected = {"main.tex": source["main"], **source["files"]}
        if frozen.exists():
            for name, data in expected.items():
                if not (frozen / name).exists() or (frozen / name).read_bytes() != data:
                    raise ValueError(f"Existing snapshot differs from source: {frozen / name}")
        else:
            for name, data in expected.items():
                (frozen / name).parent.mkdir(parents=True, exist_ok=True)
                (frozen / name).write_bytes(data)
            # Snapshot unreferenced chapter files too; they are preserved during apply.
            for path in (source_dir(volume, language) / "chapters").glob("*.tex"):
                if path.relative_to(source_dir(volume, language)).as_posix() not in expected:
                    target = frozen / "chapters" / path.name
                    target.write_bytes(path.read_bytes())
        hashes[language] = {"main_sha256": sha(source["main"]), "expanded_sha256": sha(source["expanded"]),
                            "raw_expanded_sha256": sha(source["raw_expanded"]),
                            "pdf_sha256": source["pdf_hash"],
                            "files": {name: sha(data) for name, data in source["files"].items()}}
        write_json(directory / f"pieces-{language}.json", value["pieces"])
    result = {"schema": "semantic-fragments-plan-input/v1", "order": order,
              "normalization": "legacy-input-seams/v1",
              "volume": volume["volume"], "job": volume["job"], "source_hashes": hashes,
              "warnings": {language: value["warnings"] for language, value in languages.items()},
              "input_seam_adjustments": {language: value["source"]["input_seam_adjustments"]
                                         for language, value in languages.items()},
              "entries": entries,
              "instructions": "Write only the same ordered IDs and lower-case ASCII slugs of at most 20 characters to plan.json. Source text is copied byte-for-byte; never rewrite headings, paragraphs, commands, or references."}
    write_json(directory / "plan-input.json", result)
    write_json(directory / "naming-input.json", {
        "schema": "semantic-fragments-naming-input/v1", "order": order,
        "volume": volume["volume"], "instructions": result["instructions"],
        "entries": [{key: entry[key] for key in ("id", "kind", "title_en", "title_zh", "suggested_slug")}
                    for entry in entries]})
    print(json.dumps({"order": order, "entries": len(entries), "plan_input": str(directory / "plan-input.json"),
                      "warnings": {l: len(v["warnings"]) for l, v in languages.items()}}, ensure_ascii=False))
    return result


def check_current(book, frozen, expected):
    for name, digest in {"main.tex": expected["main_sha256"], **expected["files"]}.items():
        if sha((book / name).read_bytes()) != digest or (book / name).read_bytes() != (frozen / name).read_bytes():
            raise ValueError(f"Source changed after prepare: {book / name}")
    pdf = book / "main.pdf"
    if expected["pdf_sha256"] is not None and sha(pdf.read_bytes()) != expected["pdf_sha256"]:
        raise ValueError(f"Delivery PDF changed: {pdf}")


def apply(order):
    volume = get_volume(order)
    directory = task_dir(volume)
    if (directory / "applied.json").exists():
        return verify(order)
    original = load_json(directory / "plan-input.json")
    if original.get("normalization") != "legacy-input-seams/v1":
        raise ValueError("Run prepare again to update the legacy EOF assembly contract")
    plan = load_json(directory / "plan.json")
    if plan.get("schema") != "semantic-fragments-plan/v1" or plan.get("order") != order:
        raise ValueError("Plan schema/order differs")
    entries = plan.get("entries", [])
    if [e.get("id") for e in entries] != [e["id"] for e in original["entries"]]:
        raise ValueError("Plan may only name the complete original ordered structural IDs")
    for entry in entries:
        if set(entry) != {"id", "slug"}:
            raise ValueError(f"Plan entry must contain only id and slug: {entry}")
        if not isinstance(entry["slug"], str) or len(entry["slug"]) > 20 or not SLUG.fullmatch(entry["slug"]):
            raise ValueError(f"Invalid ASCII slug or length over 20: {entry}")
    names = [f"chapters/{entry['id']}-{entry['slug']}.tex" for entry in entries]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate filenames")
    staged, report = {}, {"schema": "semantic-fragments-application/v1", "order": order, "volume": volume["volume"], "languages": {}}
    for language in ("en", "zh"):
        book, frozen = source_dir(volume, language), directory / "original" / language
        check_current(book, frozen, original["source_hashes"][language])
        source = load_source(frozen)
        pieces = load_json(directory / f"pieces-{language}.json")
        files, new_main, cursor = {}, b"", 0
        for group_index, group in enumerate(source["groups"]):
            group_pieces = [(name, piece) for name, piece in zip(names, pieces) if piece["group"] == group_index]
            # Adjacent input commands add no new whitespace to the expanded source.
            commands = b"".join(b"\\input{" + name[:-4].encode("ascii") + b"}" for name, _ in group_pieces)
            if group["include"]:
                # Include has implicit page breaks/checkpoints: preserve its exact
                # existing call and use the original chapter file as a wrapper.
                files[group["wrapper_path"]] = commands
                main_commands = source["main"][group["main_start"]:group["main_end"]]
            else:
                main_commands = commands
            new_main += source["main"][cursor:group["main_start"]] + main_commands
            cursor = group["main_end"]
            for name, piece in group_pieces:
                content = group["data"][piece["start"]:piece["end"]]
                if sha(content) != piece["sha256"]:
                    raise ValueError("Snapshot fragment hash differs")
                files[name] = content
        new_main += source["main"][cursor:]
        def expand_match(match):
            name = match[1].decode("utf-8")
            name = name if name.endswith(".tex") else name + ".tex"
            return INPUT.sub(expand_match, files[name])
        expanded = INPUT.sub(expand_match, new_main)
        if expanded != source["expanded"]:
            raise ValueError(f"Exact expanded bytes differ before writing: {language}")
        for name in files:
            if (book / name).exists() and name not in source["files"]:
                raise ValueError(f"New filename would overwrite an unrelated file: {book / name}")
        toc = {"schema": "bourbaki-semantic-fragments/v1", "order": order, "volume": volume["volume"],
               "normalization": "legacy-input-seams/v1",
               "language": language, "source_expanded_sha256": sha(expanded),
               "provenance": "fragment_provenance.json",
               "assemblies": [{"path": g["wrapper_path"], "kind": "include_assembly",
                               "preserves": "Original include page breaks and auxiliary checkpoints"}
                              for g in source["groups"] if g["include"]],
               "entries": [{**{key: entry[key] for key in ("id", "parent_id", "level", "kind", "title_en", "title_zh")},
                            "slug": naming["slug"], "path": name,
                            "source_byte_sha256": piece["sha256"], "bytes": piece["bytes"]}
                           for entry, naming, name, piece in zip(original["entries"], entries, names, pieces)],
               "warnings": original["warnings"][language]}
        stage = directory / "staged" / language
        stage.mkdir(parents=True, exist_ok=True)
        for name, content in {"main.tex": new_main, **files}.items():
            (stage / name).parent.mkdir(parents=True, exist_ok=True)
            (stage / name).write_bytes(content)
        toc_name = "tableofcontents.json" if language == "en" else "目录.json"
        write_json(stage / "chapters" / toc_name, toc)
        provenance = {"schema": "bourbaki-fragment-provenance/v1", "order": order,
                      "volume": volume["volume"], "language": language,
                      "normalization": "legacy-input-seams/v1",
                      "original_raw_expanded_sha256": original["source_hashes"][language]["raw_expanded_sha256"],
                      "original_source_files": original["source_hashes"][language]["files"],
                      "input_seam_adjustments": original["input_seam_adjustments"][language],
                      "entries": [{"id": entry["id"], "path": name, "context": entry["context"],
                                   "command": entry["command"], "starred": entry["starred"],
                                   "source_spans": entry["source_spans"][language],
                                   "assembly_spans": piece["assembly_spans"]}
                                  for entry, name, piece in zip(original["entries"], names, pieces)]}
        write_json(stage / "fragment_provenance.json", provenance)
        staged[language] = {"files": files, "main": new_main, "source": source, "stage": stage, "toc": toc_name}
        report["languages"][language] = {"expanded_sha256": sha(expanded), "pdf_sha256": original["source_hashes"][language]["pdf_sha256"],
                                           "main_sha256": sha(new_main), "fragment_count": len(pieces),
                                           "files": {name: sha(content) for name, content in files.items()}}
    # All bilingual plans/hashes were checked before any book mutation.
    for language, value in staged.items():
        book = source_dir(volume, language)
        for name, content in value["files"].items():
            (book / name).write_bytes(content)
        shutil.copyfile(value["stage"] / "chapters" / value["toc"], book / "chapters" / value["toc"])
        shutil.copyfile(value["stage"] / "fragment_provenance.json", book / "fragment_provenance.json")
        (book / "main.tex").write_bytes(value["main"])
        for name in value["source"]["files"]:
            if name not in value["files"]:
                (book / name).unlink()
        write_json(book / "semantic_fragments_manifest.json", report["languages"][language] |
                   {"schema": "semantic-fragments-manifest/v1", "order": order, "plan": str((directory / "plan.json").relative_to(ROOT)),
                    "source_snapshot": str((directory / "original" / language).relative_to(ROOT)),
                    "provenance": "fragment_provenance.json",
                    "toc": f"chapters/{value['toc']}"})
    write_json(directory / "applied.json", report)
    return verify(order)


def verify(order):
    volume, report = get_volume(order), None
    directory = task_dir(volume)
    report = load_json(directory / "applied.json")
    result = {"order": order, "volume": volume["volume"], "normalization": "legacy-input-seams/v1", "languages": {}, "ok": True}
    for language, expected in report["languages"].items():
        book = source_dir(volume, language)
        current = load_source(book)
        frozen = load_source(directory / "original" / language)
        exact = current["expanded"] == frozen["expanded"]
        pdf_exact = current["pdf_hash"] == expected["pdf_sha256"]
        files_exact = sha(current["main"]) == expected["main_sha256"] and all(
            sha((book / name).read_bytes()) == digest for name, digest in expected["files"].items())
        toc_name = "tableofcontents.json" if language == "en" else "目录.json"
        toc = load_json(book / "chapters" / toc_name)
        mappings = load_json(book / toc["provenance"])["entries"] if toc.get("provenance") else toc["entries"]
        copied_exact = True
        for entry in mappings:
            data = (book / entry["path"]).read_bytes()
            spans = entry["source_spans"][language] if isinstance(entry["source_spans"], dict) else entry["source_spans"]
            for span in spans:
                original = frozen["main"] if span["path"] == "main.tex" else frozen["files"][span["path"]]
                copied_exact &= data[span["fragment_start_byte"]:span["fragment_end_byte"]] == original[span["start_byte"]:span["end_byte"]]
        result["languages"][language] = {"normalized_expanded_bytes_identical": exact,
                                          "source_spans_copied_exactly": copied_exact, "pdf_bytes_identical": pdf_exact,
                                          "files_identical": files_exact, "expanded_sha256": sha(current["expanded"]),
                                          "fragment_count": expected["fragment_count"]}
        result["ok"] &= exact and pdf_exact and files_exact and copied_exact
    write_json(directory / "verification.json", result)
    print(json.dumps(result, ensure_ascii=False))
    if not result["ok"]:
        raise ValueError("Semantic fragment verification failed")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "apply", "verify"))
    parser.add_argument("--order", required=True, type=int)
    args = parser.parse_args()
    try:
        globals()[args.command](args.order)
    except (ValueError, KeyError, OSError, StopIteration) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
