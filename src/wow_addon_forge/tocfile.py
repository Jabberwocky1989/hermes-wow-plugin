"""WoW addon TOC parsing and lint for wow-addon-forge (created 2026-11-11).

Invariants owned by this file:
- ``parse_toc`` is the single place TOC line rules live: headers, file lines, and the header
  views (Interface, Title, Version, SavedVariables) that callers report.
- File lines are returned as written apart from the separator: ``\\`` is normalised to ``/`` and
  surrounding whitespace is stripped. [2026-11-24 T-064] Inline ``[AllowLoad…]`` /
  ``[ExcludeLoad…]`` directives are also removed from the path and kept per file in
  ``TocFile.file_directives``; nothing else is rewritten by the parser.
- [2026-11-24 T-064] ``file_skip_reason`` / ``TocFile.files_loaded`` are the single place the
  inline load conditions are judged, so ``wow_check`` and the headless loader agree.
- A repeated header key keeps the FIRST value. That is this parser's choice and is not verified
  against the live client.
- ``TocFile.header_lines`` and ``TocFile.file_lines`` record 1-based source positions for lint
  messages and are excluded from equality, so ``TocFile(headers={}, files=())`` still equals
  ``parse_toc("")`` and position data never changes what a TOC *means*.
- ``Finding`` is the shared lint-result shape (docs/SPEC-PLUGIN.md §4); other checkers
  (``luacheck``) re-export it rather than inventing a second shape.
- ``lint_toc`` touches the disk only through the injected ``FileSystem``, so tests run it
  against an in-memory tree on every OS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # [2026-11-11 T-006] env.py imports this module at top level, so the FileSystem protocol
    # is imported for type checking only; importing it at runtime here would be circular.
    from wow_addon_forge.env import FileSystem

# [2026-11-11 T-003] Colour escapes are '|c' plus 8 hex digits and the reset '|r'; matching is
# case-insensitive so '|C' and '|R' written by hand are removed too.
_UI_ESCAPES = re.compile(r"\|c[0-9a-f]{8}|\|r", re.IGNORECASE)


def strip_ui_escapes(text: str) -> str:
    """Remove WoW UI colour escapes from ``text`` and strip surrounding whitespace.

    Why this exists:
        Titles carry display markup such as ``Questie|cFF00FF00 v12.6.4|r``; reports want the
        plain text a reader sees.

    Args:
        text: The raw text, usually a ``## Title:`` header value.

    Returns:
        The text with every ``|c`` + 8-hex-digit sequence and every ``|r`` removed
        (case-insensitive), then whitespace-stripped.

    Failure modes:
        None; text without escapes is returned stripped but otherwise unchanged.
    """
    return _UI_ESCAPES.sub("", text).strip()


@dataclass(frozen=True)
class Finding:
    """One lint or check result, in the wire shape of docs/SPEC-PLUGIN.md §4.

    Why this exists:
        Every checker in the plugin reports problems the same way, so a caller renders TOC,
        Lua and later findings through one path instead of per-checker shapes.

    Args:
        severity: ``"error"`` (the addon will not load or behave as written) or ``"warning"``
            (it loads, but something is probably unintended).
        code: Stable machine-readable rule name, e.g. ``"missing-file"``.
        file: The file the finding is reported against (the TOC for TOC rules, even when the
            message names another file).
        line: 1-based line number in ``file``, or ``None`` when no single line is to blame.
        message: What is wrong, in one sentence a person can act on.
        hint: How to fix it, in one sentence.

    Failure modes:
        None at construction; ``to_dict`` builds plain JSON-serialisable data only.
    """

    severity: str
    code: str
    file: str
    line: int | None
    message: str
    hint: str

    def to_dict(self) -> dict[str, Any]:
        """Return this finding as plain JSON-serialisable data.

        Why this exists:
            Tool results cross the Hermes boundary as JSON, so the wire shape is pinned in
            one place.

        Returns:
            A dict with exactly the keys ``severity``, ``code``, ``file``, ``line``,
            ``message`` and ``hint``, in that order (docs/SPEC-PLUGIN.md §4).

        Failure modes:
            None.
        """
        return {
            "severity": self.severity,
            "code": self.code,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "hint": self.hint,
        }


# [2026-11-11 T-006] The 21 reserved words of Lua 5.1 (the client's Lua). `goto` is absent on
# purpose: it only became a keyword in Lua 5.2, so `goto` is a legal 5.1 identifier even
# though modern-Lua habits say otherwise.
LUA_51_KEYWORDS: frozenset[str] = frozenset(
    {
        "and",
        "break",
        "do",
        "else",
        "elseif",
        "end",
        "false",
        "for",
        "function",
        "if",
        "in",
        "local",
        "nil",
        "not",
        "or",
        "repeat",
        "return",
        "then",
        "true",
        "until",
        "while",
    }
)

_LUA_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_valid_lua_identifier(name: str) -> bool:
    """Return whether ``name`` is a usable Lua 5.1 variable name.

    Why this exists:
        ``SavedVariables`` entries become global variable names in the client; a name Lua
        cannot hold is silently useless in game, so the spelling rule is owned here once.

    Args:
        name: The candidate name, exactly as written (no stripping is done here).

    Returns:
        ``True`` when ``name`` matches ``^[A-Za-z_][A-Za-z0-9_]*$`` and is not one of the
        21 Lua 5.1 reserved words in ``LUA_51_KEYWORDS``.

    Failure modes:
        None; an unusable name gives ``False``.
    """
    return bool(_LUA_IDENTIFIER.match(name)) and name not in LUA_51_KEYWORDS


# [2026-11-24 T-064] Windows device names, which Microsoft's file-naming rules reserve with or
# without an extension (any case). Observed on Windows 11 build 26300: a folder named `NUL`
# could not be created, while `CON`, `AUX` and `COM1` could; other Windows versions were not
# tried. The superscript-digit forms (`COM¹`) are not in this set.
_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{digit}" for digit in range(10)}
    | {f"LPT{digit}" for digit in range(10)}
)


def is_windows_reserved_name(name: str) -> bool:
    """Return whether Windows reserves ``name`` (one path segment) for a device.

    Why this exists:
        An addon is shared between players on every OS, so an addon built on macOS or
        Linux with a folder or file named ``Aux`` may be impossible to install for a guild
        mate on Windows. The rule lives here once so ``wow_new`` and ``lint_toc`` agree.

    Args:
        name: One path segment, such as ``"Aux"`` or ``"Nul.lua"``.

    Returns:
        ``True`` when the part before the first ``.``, with trailing spaces removed and
        compared case-insensitively, is a reserved device name (``CON``, ``PRN``, ``AUX``,
        ``NUL``, ``COM0``-``COM9``, ``LPT0``-``LPT9``).

    Failure modes:
        None; any other name gives ``False``.
    """
    return name.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_RESERVED_NAMES


# [2026-11-24 T-064] Game types WoW: Forever answers to, per warcraft.wiki.gg "TOC format"
# (read 2026-11-24): the game type is listed as both `forever` and `camelot`, and both
# belong to the `mainline` family. A `[AllowLoadGameType mainline]` file therefore DOES load
# in Forever, which a check against `camelot` alone would get wrong.
FOREVER_GAME_TYPES: frozenset[str] = frozenset({"forever", "camelot", "mainline"})

# [2026-11-24 T-064] Every game type and family the same wiki table lists. The wiki warns
# that an AllowLoadGameType list naming ONLY types the client does not recognise is
# satisfied (the file loads). Which names the Forever client recognises is not verified;
# this table is the assumption, so a list naming only future types loads, as documented.
KNOWN_GAME_TYPES: frozenset[str] = frozenset(
    {
        "standard",
        "forever",
        "camelot",
        "mists",
        "cata",
        "wrath",
        "tbc",
        "vanilla",
        "plunderstorm",
        "wowhack",
        "mainline",
        "classic",
    }
)

# [2026-11-24 T-064] Inline directives are `[Name]` or `[Name value, value]` and may appear
# several times on one line. [2026-11-24 T-073] The full list on warcraft.wiki.gg "TOC
# format" (read 2026-11-24), lower-cased: T-064 had missed LoadIntoEnvironment and
# Bootstrap, so a line carrying either was checked as a file literally named with it.
KNOWN_INLINE_DIRECTIVES: frozenset[str] = frozenset(
    {
        "allowload",
        "allowloadgametype",
        "excludeloadgametype",
        "allowloadtextlocale",
        "allowloadenvironment",
        "bootstrap",
        "loadintoenvironment",
    }
)

# [2026-11-24 T-064] Only bracket groups named AllowLoad…/ExcludeLoad… are taken as
# directives. Any other `[...]` stays part of the path exactly as before, so a file name
# that really contains brackets is not rewritten by a guess. [2026-11-24 T-073] The two
# directives outside that pattern, LoadIntoEnvironment and Bootstrap, are named exactly.
_INLINE_DIRECTIVE = re.compile(
    r"\[\s*((?:Allow|Exclude)Load\w*|LoadIntoEnvironment|Bootstrap)(?:\s+([^\]]*))?\]",
    re.IGNORECASE,
)

# [2026-11-24 T-073] TOC path variables (wiki "TOC format" → Variables): `[Family]`,
# `[Game]`, `[TextLocale]` and `[RegionalTextLocale]` expand inside a file line. The Forever
# client's values come from Ketho/BlizzardInterfaceResources' README, a dump taken in that
# client (forever branch, build 70938): `TOC [Family] => "Mainline"`, `TOC [Game] =>
# "Camelot"`. The wiki notes Camelot may later become Forever; this table is the one place
# to change then.
FOREVER_TOC_VARIABLES: dict[str, str] = {"family": "Mainline", "game": "Camelot"}

# [2026-11-24 T-073] Client text locales, from the wiki's GetAvailableLocaleInfo table (read
# 2026-11-24); `[TextLocale]` expands to one of these. `[RegionalTextLocale]` can also give
# enGB or ptPT (the wiki's examples for the EU region).
CLIENT_TEXT_LOCALES: tuple[str, ...] = (
    "enUS",
    "koKR",
    "frFR",
    "deDE",
    "zhCN",
    "esES",
    "zhTW",
    "esMX",
    "ruRU",
    "ptBR",
    "itIT",
)
REGIONAL_TEXT_LOCALES: tuple[str, ...] = (*CLIENT_TEXT_LOCALES, "enGB", "ptPT")

_TOC_VARIABLE = re.compile(r"\[(Family|Game|TextLocale|RegionalTextLocale)\]", re.IGNORECASE)


def expand_toc_variables(path: str, *, locale: str | None) -> str:
    """Expand the TOC path variables in one file line for the Forever client.

    Why this exists:
        [2026-11-24 T-073] `Locale/[TextLocale].lua` names a different file per client; the
        headless loader needs the one its client loads, and the checker the set it might.

    Args:
        path: A file line as ``TocFile.files`` holds it.
        locale: The client text locale, e.g. ``"enUS"``; None leaves ``[TextLocale]`` and
            ``[RegionalTextLocale]`` in place (``toc_path_variants`` expands those).

    Returns:
        The path with ``[Family]`` and ``[Game]`` replaced by ``FOREVER_TOC_VARIABLES``
        and, when ``locale`` is given, both locale variables replaced by it. Using the same
        value for ``[RegionalTextLocale]`` is right for an NA client (enUS gives enUS) and
        is this function's assumption for the rest.

    Failure modes:
        None; a path without variables comes back unchanged.
    """

    def replace(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        if key in FOREVER_TOC_VARIABLES:
            return FOREVER_TOC_VARIABLES[key]
        return match.group(0) if locale is None else locale

    return _TOC_VARIABLE.sub(replace, path)


def toc_path_variants(path: str) -> tuple[str, ...]:
    """Return every path a file line can name on a Forever client, across locales.

    Why this exists:
        [2026-11-24 T-073] A static check covers every player's locale: a listed
        `Locale/[TextLocale].lua` is present when any locale's file exists, and each of
        those files is listed, not stray.

    Args:
        path: A file line as ``TocFile.files`` holds it.

    Returns:
        The expanded paths, without duplicates, in locale order; one path when the line has
        no locale variable. With both locale variables on one line, each variant puts the
        same locale in both (pairs such as enUS with enGB are not generated).

    Failure modes:
        None.
    """
    fixed = expand_toc_variables(path, locale=None)
    lowered = fixed.lower()
    if "[regionaltextlocale]" in lowered:
        locales = REGIONAL_TEXT_LOCALES
    elif "[textlocale]" in lowered:
        locales = CLIENT_TEXT_LOCALES
    else:
        return (fixed,)
    return tuple(dict.fromkeys(expand_toc_variables(fixed, locale=code) for code in locales))


@dataclass(frozen=True)
class InlineDirective:
    """One inline TOC directive attached to a file line, such as ``[AllowLoadGameType vanilla]``.

    Why this exists:
        A TOC file line may carry load conditions after its path (warcraft.wiki.gg "TOC
        format"). Keeping them apart from the path lets callers check the real file and
        decide whether the client would load it.

    Args:
        name: The directive name as written, e.g. ``"AllowLoadGameType"``.
        values: The comma-separated values, stripped, in order; empty for ``[AllowLoad]``.

    Failure modes:
        None at construction.
    """

    name: str
    values: tuple[str, ...]

    @property
    def known(self) -> bool:
        """Return whether the name is one of ``KNOWN_INLINE_DIRECTIVES`` (case-insensitive)."""
        return self.name.lower() in KNOWN_INLINE_DIRECTIVES


def _split_inline_directives(line: str) -> tuple[str, tuple[InlineDirective, ...]]:
    directives: list[InlineDirective] = []

    def take(match: re.Match[str]) -> str:
        raw_values = match.group(2) or ""
        values = tuple(value.strip() for value in raw_values.split(",") if value.strip())
        directives.append(InlineDirective(name=match.group(1), values=values))
        return ""

    path = _INLINE_DIRECTIVE.sub(take, line).strip()
    return path, tuple(directives)


def file_skip_reason(
    directives: tuple[InlineDirective, ...],
    *,
    game_types: frozenset[str] = FOREVER_GAME_TYPES,
    locale: str | None = None,
) -> str | None:
    """Return why the client would NOT load a TOC file line, or ``None`` when it would.

    Why this exists:
        The headless loader and ``wow_check`` must load and check the files the real client
        loads, not every line of the TOC; one function owns the condition rules.

    Args:
        directives: The file's inline directives (``TocFile.directives_for``).
        game_types: The client's game type and family names, lower-case.
        locale: The client text locale such as ``"enUS"``, or ``None`` to ignore
            ``AllowLoadTextLocale`` (a static check covers every player's locale).

    Returns:
        ``None`` when every condition admits the client, else one sentence naming the
        directive that excludes it. Rules, per warcraft.wiki.gg "TOC format" (and
        [2026-11-24 T-073] ``LoadIntoEnvironment Secure`` excludes, since only Blizzard code
        loads there, while ``Bootstrap`` never excludes: the headless loader does not model
        LoadOnDemand bootstrapping, so such a file is loaded and checked like any other,
        which is unverified in game):
        ``AllowLoadGameType`` admits a listed type or family, and also a list whose names
        are all outside ``KNOWN_GAME_TYPES``; ``ExcludeLoadGameType`` refuses a listed type
        or family; ``AllowLoadTextLocale`` admits a listed locale; ``AllowLoad`` admits
        ``Game`` or ``Both``; ``AllowLoadEnvironment`` admits ``Global`` (third-party addon
        code runs in the global environment — inferred from the wiki, not verified in game).
        Unknown directive names never exclude a file; ``lint_toc`` warns about them instead.

    Failure modes:
        None.
    """
    for directive in directives:
        key = directive.name.lower()
        values = {value.lower() for value in directive.values}
        shown = f"[{directive.name} {', '.join(directive.values)}]".replace(" ]", "]")
        if key == "allowloadgametype":
            if values & KNOWN_GAME_TYPES and not values & game_types:
                return f"{shown} does not include this client's game type."
        elif key == "excludeloadgametype":
            if values & game_types:
                return f"{shown} excludes this client's game type."
        elif key == "allowloadtextlocale":
            if locale is not None and values and locale.lower() not in values:
                return f"{shown} does not include the client locale {locale}."
        elif key == "allowload":
            if values and not values & {"game", "both"}:
                return f"{shown} limits the file to the glue screen, where addons never load."
        elif key == "allowloadenvironment" and values and "global" not in values:
            return f"{shown} limits the file to an environment addon code does not run in."
        elif key == "loadintoenvironment" and values and "global" not in values:
            return f"{shown} loads the file into an environment only Blizzard code can use."
    return None


def _split_saved_variables(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    return tuple(token.strip() for token in raw.split(",") if token.strip())


@dataclass(frozen=True)
class TocFile:
    """The parsed facts of one TOC file.

    Why this exists:
        Callers report title, version and Interface numbers without re-reading raw lines, so
        the header spelling rules live here exactly once.

    Args:
        headers: ``## Key: Value`` lines, key and value stripped, keys stored exactly as
            written (case-sensitive); a repeated key keeps the first value.
        files: Every non-blank, non-comment line in order, stripped, with ``\\`` normalised
            to ``/`` and inline directives removed ([2026-11-24 T-064]). Path variables such
            as ``[TextLocale]`` stay as written ([2026-11-24 T-073]); ``files_loaded`` and
            ``toc_path_variants`` expand them.
        header_lines: 1-based line number of the first occurrence of each header key, for
            lint positions. Excluded from equality: where a header sits is not what it means.
        file_lines: ``(normalised_file_path, line_no)`` for each entry in ``files``, in the
            same order. Excluded from equality for the same reason as ``header_lines``.
        directive_only_lines: [2026-11-24 T-073] 1-based numbers of lines that hold only
            inline directives; excluded from equality like the other positions.
        file_directives: ``{index into files: inline directives}`` for the file lines that
            carry any ([2026-11-24 T-064]). Part of equality: a condition changes whether the
            client loads the file. Lines without directives have no key, so a hand-built
            ``TocFile(headers={}, files=("a.lua",))`` still equals ``parse_toc("a.lua")``.

    Failure modes:
        None at construction; absent or malformed headers surface as empty or ``None`` views
        through the properties below.
    """

    headers: dict[str, str]
    files: tuple[str, ...]
    # [2026-11-11 T-006] compare=False keeps the pre-T-006 equality contract: a hand-built
    # TocFile(headers={}, files=()) must still equal parse_toc(""), positions or not.
    header_lines: dict[str, int] = field(default_factory=dict, compare=False)
    file_lines: tuple[tuple[str, int], ...] = field(default_factory=tuple, compare=False)
    file_directives: dict[int, tuple[InlineDirective, ...]] = field(default_factory=dict)
    # [2026-11-24 T-073] Line numbers of lines holding only inline directives, with no path;
    # parse_toc drops them, so lint_toc reads this to say so instead of losing them silently.
    directive_only_lines: tuple[int, ...] = field(default_factory=tuple, compare=False)

    def directives_for(self, index: int) -> tuple[InlineDirective, ...]:
        """Return the inline directives of ``files[index]`` (empty when it has none).

        Why this exists:
            Callers walk ``files`` by position; this keeps the sparse ``file_directives``
            map an implementation detail.
        """
        return self.file_directives.get(index, ())

    def files_loaded(
        self,
        *,
        game_types: frozenset[str] = FOREVER_GAME_TYPES,
        locale: str | None = None,
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        """Split ``files`` into the ones the client loads and the ones it skips.

        Why this exists:
            ``wow_check`` and the headless loader must agree on which files a client loads;
            both call this instead of re-walking the conditions.

        Args:
            game_types: The client's game type and family names (``file_skip_reason``).
            locale: The client locale, or ``None`` to admit every locale.

        Returns:
            ``(loaded, skipped)``: loaded paths in TOC order, and ``(path, reason)`` pairs.
            [2026-11-24 T-073] Paths come back through ``expand_toc_variables``: with a
            locale, every variable is expanded; with ``None``, ``[TextLocale]`` and
            ``[RegionalTextLocale]`` stay for the caller to widen with ``toc_path_variants``.

        Failure modes:
            None.
        """
        loaded: list[str] = []
        skipped: list[tuple[str, str]] = []
        for index, path in enumerate(self.files):
            reason = file_skip_reason(
                self.directives_for(index), game_types=game_types, locale=locale
            )
            expanded = expand_toc_variables(path, locale=locale)
            if reason is None:
                loaded.append(expanded)
            else:
                skipped.append((expanded, reason))
        return tuple(loaded), tuple(skipped)

    @property
    def interface(self) -> tuple[int, ...]:
        """Return the Interface numbers from the ``Interface`` header.

        Why this exists:
            Whether an addon supports a client is decided by membership in this tuple, so
            the comma-and-digits parsing is owned here.

        Returns:
            One integer per comma-separated all-digit token, in order, e.g.
            ``## Interface: 16001, 11507`` becomes ``(16001, 11507)``. Tokens that are not
            all digits are dropped, and a missing header gives ``()``.

        Failure modes:
            None; a malformed header degrades to fewer (or zero) numbers.
        """
        raw = self.headers.get("Interface")
        if raw is None:
            return ()
        numbers: list[int] = []
        for token in raw.split(","):
            stripped = token.strip()
            if stripped.isdigit():
                numbers.append(int(stripped))
        return tuple(numbers)

    @property
    def title(self) -> str | None:
        """Return the plain-text ``Title`` header.

        Why this exists:
            Reports show the title a reader sees in game, without its colour markup.

        Returns:
            The title passed through ``strip_ui_escapes``, or ``None`` when the header is
            absent or empty after stripping.

        Failure modes:
            None.
        """
        raw = self.headers.get("Title")
        if raw is None:
            return None
        return strip_ui_escapes(raw) or None

    @property
    def version(self) -> str | None:
        """Return the ``Version`` header.

        Why this exists:
            Reports pair an addon name with the version its TOC declares.

        Returns:
            The version string, or ``None`` when the header is absent or empty.

        Failure modes:
            None.
        """
        raw = self.headers.get("Version")
        if raw is None:
            return None
        return raw.strip() or None

    @property
    def saved_variables(self) -> tuple[str, ...]:
        """Return the account-wide saved-variable names from ``SavedVariables``.

        Why this exists:
            Later checks must know which globals the client persists for this addon, so the
            comma-splitting of the header is owned here.

        Returns:
            The comma-separated tokens of the ``SavedVariables`` header, stripped, with
            empty tokens dropped, in order. A missing header gives ``()``. Tokens are
            returned as written even when invalid; validity is ``lint_toc``'s judgement.

        Failure modes:
            None.
        """
        return _split_saved_variables(self.headers.get("SavedVariables"))

    @property
    def saved_variables_per_character(self) -> tuple[str, ...]:
        """Return the per-character saved-variable names from the matching header.

        Why this exists:
            The per-character names persist separately from the account-wide ones, and
            callers that seed or check saved data need to tell them apart.

        Returns:
            The comma-separated tokens of the ``SavedVariablesPerCharacter`` header,
            stripped, with empty tokens dropped, in order. A missing header gives ``()``.

        Failure modes:
            None.
        """
        return _split_saved_variables(self.headers.get("SavedVariablesPerCharacter"))

    @property
    def all_saved_variables(self) -> tuple[str, ...]:
        """Return every saved-variable name this TOC declares, deduplicated.

        Why this exists:
            Checks that only ask "is this global persisted at all?" should not have to merge
            the two headers (or double-count a name declared in both) themselves.

        Returns:
            ``saved_variables`` followed by any ``saved_variables_per_character`` names not
            already present, order preserved.

        Failure modes:
            None.
        """
        return tuple(dict.fromkeys((*self.saved_variables, *self.saved_variables_per_character)))


def parse_toc(text: str) -> TocFile:
    """Parse TOC text into a ``TocFile``.

    Why this exists:
        The single entry point every caller uses, so the line rules below are the whole
        contract.

    Args:
        text: The full TOC file contents. One leading BOM character is removed if present.

    Returns:
        The parsed ``TocFile``, including 1-based ``header_lines`` / ``file_lines`` positions
        counted on the BOM-stripped text. Blank lines and single-``#`` comments are skipped;
        a ``##`` line is a header only when it contains ``:`` after the ``##`` (an empty key
        is ignored, and a ``##`` line without ``:`` is ignored). A repeated key keeps the
        FIRST value; that is this parser's choice, not verified against the live client.

    Failure modes:
        None; malformed lines are ignored or treated as file lines by the rules above.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    headers: dict[str, str] = {}
    header_lines: dict[str, int] = {}
    files: list[str] = []
    file_lines: list[tuple[str, int]] = []
    file_directives: dict[int, tuple[InlineDirective, ...]] = {}
    directive_only_lines: list[int] = []
    # [2026-11-11 T-006] Line numbers are counted after the BOM is stripped, so a BOM can
    # never shift a reported position; the BOM sits on line 1 either way, but counting the
    # stripped text keeps the rule true by construction rather than by luck.
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("##"):
            key, separator, value = line[2:].partition(":")
            if not separator:
                continue
            stripped_key = key.strip()
            # [2026-11-24 T-073] Header lines take inline directives too (wiki "TOC format":
            # `## Title: My Cool AddOn in Classic [AllowLoadGameType classic]`). They come off
            # the value, and a line whose conditions exclude Forever does not apply to it.
            # Locale conditions are ignored here (locale None): a header has one value.
            header_value, header_directives = _split_inline_directives(value)
            if header_directives and file_skip_reason(header_directives) is not None:
                continue
            value = header_value
            # [2026-11-11 T-003] First value wins for a repeated key; our choice, not verified
            # against the live client.
            if stripped_key and stripped_key not in headers:
                headers[stripped_key] = value.strip()
                header_lines[stripped_key] = line_no
            continue
        if line.startswith("#"):
            continue
        # [2026-11-24 T-064] Inline load conditions come off before the separator is
        # normalised, so `Core\Retail.lua [AllowLoadGameType mainline]` lists the real file
        # `Core/Retail.lua` and keeps its condition beside it instead of inside the name.
        path, directives = _split_inline_directives(line)
        if not path:
            # [2026-11-24 T-073] Only directives, no file: kept by number for lint_toc.
            directive_only_lines.append(line_no)
            continue
        normalised = path.replace("\\", "/")
        if directives:
            file_directives[len(files)] = directives
        files.append(normalised)
        file_lines.append((normalised, line_no))
    return TocFile(
        headers=headers,
        files=tuple(files),
        header_lines=header_lines,
        file_lines=tuple(file_lines),
        file_directives=file_directives,
        directive_only_lines=tuple(directive_only_lines),
    )


def _walk_lua_files(fs: FileSystem, addon_dir: str) -> list[str]:
    # [2026-11-11 T-006] Iterative walk over the FileSystem seam: no os.walk (tests inject an
    # in-memory tree), and hidden folders plus Tests/ are pruned because addon test files are
    # deliberately never listed in a TOC. Paths come back relative with "/" separators so
    # they compare directly against the normalised TOC file lines.
    found: list[str] = []
    stack: list[tuple[str, str]] = [(addon_dir, "")]
    while stack:
        dir_path, prefix = stack.pop()
        try:
            entries = fs.list_dir(dir_path)
        except OSError:
            continue
        for name in sorted(entries, reverse=True):
            full_path = fs.join(dir_path, name)
            relative = f"{prefix}{name}"
            if fs.is_dir(full_path):
                if name.startswith(".") or name.lower() == "tests":
                    continue
                stack.append((full_path, f"{relative}/"))
            elif fs.is_file(full_path) and name.lower().endswith(".lua"):
                found.append(relative)
    return sorted(found)


def lint_toc(
    toc: TocFile,
    *,
    toc_filename: str,
    addon_dir: str,
    fs: FileSystem,
    folder_name: str | None = None,
    client_interface: int = 16001,
) -> list[Finding]:
    """Lint one parsed TOC against the addon folder on disk.

    Why this exists:
        The client fails silently on TOC mistakes a machine can catch exactly: a TOC whose
        name does not match its folder never loads, a wrong Interface number marks the addon
        out of date, an unusable SavedVariables name never persists, and a listed-but-missing
        file aborts the load. One function owns those rules so every caller reports alike.

    Args:
        toc: The parsed TOC, from ``parse_toc`` (a hand-built ``TocFile`` without position
            data lints with line 1 fallbacks).
        toc_filename: The TOC's file name, e.g. ``MyAddon.toc``; findings are reported
            against it.
        addon_dir: Path of the addon folder, as the injected ``fs`` understands it.
        fs: The filesystem seam all disk access goes through.
        folder_name: The folder name the TOC must match. When ``None`` it is derived from
            the last ``/``-separated component of ``addon_dir``.
        client_interface: The client Interface number the TOC must declare support for.

    Returns:
        Findings in rule order: ``toc-name-mismatch``, ``toc-interface``,
        ``invalid-savedvar``, ``invalid-file-path`` / ``missing-file``, then the warnings
        ``unknown-inline-directive`` and ``windows-reserved-name`` ([2026-11-24 T-064]),
        then ``unlisted-file``. An empty list means the TOC passed every rule.

    Failure modes:
        None raised for a malformed TOC or an unreadable folder: rule violations become
        findings, and directories that cannot be listed are skipped during the
        unlisted-file walk.
    """
    findings: list[Finding] = []
    expected_folder = (
        folder_name
        if folder_name is not None
        else addon_dir.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    )

    toc_stem = toc_filename[:-4] if toc_filename.lower().endswith(".toc") else toc_filename
    if toc_stem != expected_folder:
        findings.append(
            Finding(
                severity="error",
                code="toc-name-mismatch",
                file=toc_filename,
                line=1,
                message=(
                    f"TOC file '{toc_filename}' does not match its addon folder "
                    f"'{expected_folder}'; the client only loads a TOC named after its folder."
                ),
                hint=f"Rename the TOC to '{expected_folder}.toc' or rename the folder.",
            )
        )

    if client_interface not in toc.interface:
        declared = ", ".join(str(number) for number in toc.interface) or "none"
        findings.append(
            Finding(
                severity="error",
                code="toc-interface",
                file=toc_filename,
                line=toc.header_lines.get("Interface", 1),
                message=(
                    f"## Interface does not include the client interface "
                    f"{client_interface} (declared: {declared})."
                ),
                hint=f"Add {client_interface} to the ## Interface header.",
            )
        )

    for header_key in ("SavedVariables", "SavedVariablesPerCharacter"):
        raw = toc.headers.get(header_key)
        # [2026-11-11 T-006] An absent or blank header declares nothing, which is legal; only
        # a header that names something unusable (or leaves an empty slot between commas in
        # a non-blank value) is an error, so `## SavedVariables:` alone stays quiet while a
        # trailing comma in `Foo,` does not.
        if raw is None or raw.strip() == "":
            continue
        for token in raw.split(","):
            stripped = token.strip()
            if stripped and is_valid_lua_identifier(stripped):
                continue
            shown = stripped if stripped else "(empty entry)"
            findings.append(
                Finding(
                    severity="error",
                    code="invalid-savedvar",
                    file=toc_filename,
                    line=toc.header_lines.get(header_key, 1),
                    message=(
                        f"## {header_key} entry {shown!r} is not a valid Lua 5.1 "
                        f"identifier, so the client cannot persist it."
                    ),
                    hint=(
                        "Use letters, digits and underscores, do not start with a digit, "
                        "and do not use a Lua keyword."
                    ),
                )
            )

    listed = toc.file_lines if toc.file_lines else tuple((f, 1) for f in toc.files)
    for rel_path, line_no in listed:
        segments = rel_path.split("/")
        # [2026-11-11 T-006] The path is judged as written, before any join: an absolute path,
        # a ".." segment or an empty segment would let a TOC reach outside its own folder,
        # which the client never loads and a checker must never follow onto the real disk.
        is_absolute = rel_path.startswith("/") or (len(rel_path) >= 2 and rel_path[1] == ":")
        if is_absolute or ".." in segments or "" in segments:
            findings.append(
                Finding(
                    severity="error",
                    code="invalid-file-path",
                    file=toc_filename,
                    line=line_no,
                    message=(
                        f"Listed file '{rel_path}' is not a relative path inside the addon folder."
                    ),
                    hint="List files relative to the addon folder, with no '..'.",
                )
            )
            continue
        # [2026-11-24 T-073] A line with path variables is present when any variant exists.
        if not any(
            fs.is_file(fs.join(addon_dir, *variant.split("/")))
            for variant in toc_path_variants(rel_path)
        ):
            findings.append(
                Finding(
                    severity="error",
                    code="missing-file",
                    file=toc_filename,
                    line=line_no,
                    message=f"Listed file '{rel_path}' does not exist in the addon folder.",
                    hint="Create the file or remove its line from the TOC.",
                )
            )

    # [2026-11-24 T-064] An unrecognised AllowLoad…/ExcludeLoad… name is most likely a typo
    # (`[AllowLoadGametypes …]`): the file still loads everywhere, which is rarely what the
    # author meant, so it is a warning rather than silence.
    for index, (rel_path, line_no) in enumerate(listed):
        for directive in toc.directives_for(index):
            if directive.known:
                continue
            findings.append(
                Finding(
                    severity="warning",
                    code="unknown-inline-directive",
                    file=toc_filename,
                    line=line_no,
                    message=(
                        f"Listed file '{rel_path}' carries an unknown inline directive "
                        f"[{directive.name}], so it does not restrict where the file loads."
                    ),
                    hint=(
                        "Use AllowLoad, AllowLoadGameType, ExcludeLoadGameType, "
                        "AllowLoadTextLocale, AllowLoadEnvironment, Bootstrap or "
                        "LoadIntoEnvironment."
                    ),
                )
            )

    # [2026-11-24 T-064] Reported on every OS on purpose: the author is often on macOS while
    # guild mates install on Windows, where these names open a device instead of a file.
    reserved_targets = [(expected_folder, 1, "Addon folder")]
    reserved_targets += [
        (segment, line_no, f"Listed file '{rel_path}'")
        for rel_path, line_no in listed
        for segment in rel_path.split("/")
    ]
    for name, line_no, subject in reserved_targets:
        if not is_windows_reserved_name(name):
            continue
        findings.append(
            Finding(
                severity="warning",
                code="windows-reserved-name",
                file=toc_filename,
                line=line_no,
                message=(
                    f"{subject} uses '{name}', a device name Windows reserves; players on "
                    "Windows may be unable to install or load it."
                ),
                hint="Rename it to something other than CON, PRN, AUX, NUL, COM0-9 or LPT0-9.",
            )
        )

    # [2026-11-24 T-073] A file line with nothing but directives names no file: the client
    # has nothing to load there, which is rarely what the author meant.
    for line_no in toc.directive_only_lines:
        findings.append(
            Finding(
                severity="warning",
                code="directive-only-line",
                file=toc_filename,
                line=line_no,
                message="This line holds only inline directives and names no file to load.",
                hint="Put the file name before the directives, or delete the line.",
            )
        )

    # [2026-11-24 T-073] Every locale's file a variable line can name counts as listed.
    listed_set = {
        variant for listed_path in toc.files for variant in toc_path_variants(listed_path)
    }
    for relative in _walk_lua_files(fs, addon_dir):
        if relative not in listed_set:
            findings.append(
                Finding(
                    severity="warning",
                    code="unlisted-file",
                    file=toc_filename,
                    line=None,
                    message=(
                        f"Lua file '{relative}' exists in the addon folder but is not "
                        f"listed in the TOC, so the client will never load it."
                    ),
                    hint=f"Add '{relative}' to {toc_filename} or remove the file.",
                )
            )

    return findings
