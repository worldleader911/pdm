from __future__ import annotations

import argparse
import hashlib
import shlex
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from pdm.exceptions import PdmUsageError
from pdm.formats.base import make_array
from pdm.models.requirements import FileRequirement, Requirement, parse_requirement
from pdm.utils import expand_env_vars_in_auth

if TYPE_CHECKING:
    from argparse import Namespace
    from os import PathLike

    from pdm.models.candidates import Candidate
    from pdm.project import Project


class RequirementParser:
    """Reference:
    https://pip.pypa.io/en/stable/reference/requirements-file-format/
    """

    # TODO: support no_binary, only_binary, prefer_binary, pre and no_index

    def __init__(self) -> None:
        self.requirements: list[Requirement] = []
        self.index_url: str | None = None
        self.extra_index_urls: list[str] = []
        self.no_index: bool = False
        self.find_links: list[str] = []
        self.trusted_hosts: list[str] = []
        parser = argparse.ArgumentParser()
        parser.add_argument("--index-url", "-i")
        parser.add_argument("--no-index", action="store_true")
        parser.add_argument("--extra-index-url")
        parser.add_argument("--find-links", "-f")
        parser.add_argument("--trusted-host")
        parser.add_argument("-e", "--editable", nargs="+")
        parser.add_argument("-r", "--requirement")
        self._parser = parser

    def _clean_line(self, line: str) -> str:
        """Strip the surrounding whitespaces and comment from the line"""
        line = line.strip()
        if line.startswith("#"):
            return ""
        return line.split(" #", 1)[0].strip()

    def _parse_line(self, filename: str, line: str) -> None:
        if not line.startswith("-"):
            # Starts with a requirement, just ignore all per-requirement options
            req_string = line.split(" -", 1)[0].strip()
            req = parse_requirement(req_string)
            if not req.name:
                assert isinstance(req, FileRequirement)
                req.name = req.guess_name()
            self.requirements.append(req)
            return
        args, _ = self._parser.parse_known_args(shlex.split(line))
        if args.index_url:
            self.index_url = args.index_url
        if args.no_index:
            self.no_index = args.no_index
        if args.extra_index_url:
            self.extra_index_urls.append(args.extra_index_url)
        if args.find_links:
            self.find_links.append(args.find_links)
        if args.trusted_host:
            self.trusted_hosts.append(args.trusted_host)
        if args.editable:
            self.requirements.append(parse_requirement(" ".join(args.editable), True))
        if args.requirement:
            referenced_requirements = str(Path(filename).parent.joinpath(args.requirement))
            self.parse(referenced_requirements)

    def parse(self, filename: str) -> None:
        with open(filename, encoding="utf-8") as f:
            this_line = ""
            for line in filter(None, map(self._clean_line, f)):
                if line.endswith("\\"):
                    this_line += line[:-1].rstrip() + " "
                    continue
                this_line += line
                self._parse_line(filename, this_line)
                this_line = ""
            if this_line:
                self._parse_line(filename, this_line)


def check_fingerprint(project: Project, filename: PathLike) -> bool:
    from pdm.compat import tomllib

    with open(filename, "rb") as fp:
        try:
            tomllib.load(fp)
        except ValueError:
            # the file should be a requirements.txt
            # if it's not a TOML document nor py script.
            return Path(filename).suffix not in (".py",)
        else:
            return False


def _is_url_trusted(url: str, trusted_hosts: list[str]) -> bool:
    parsed = urllib.parse.urlparse(url)
    netloc, host = parsed.netloc, parsed.hostname

    for trusted in trusted_hosts:
        if trusted in (host, netloc):
            return True
    return False


def convert_url_to_source(url: str, name: str | None, trusted_hosts: list[str], type: str = "index") -> dict[str, Any]:
    if not name:
        name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]
    source = {
        "name": name,
        "url": url,
        "verify_ssl": not _is_url_trusted(url, trusted_hosts),
    }
    if type != "index":
        source["type"] = type
    return source


def convert(project: Project, filename: PathLike, options: Namespace) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    parser = RequirementParser()
    parser.parse(str(filename))
    backend = project.backend

    deps = make_array([], True)
    dev_deps = make_array([], True)

    for req in parser.requirements:
        if req.is_file_or_url:
            req.relocate(backend)  # type: ignore[attr-defined]
        if req.editable or options.dev:
            dev_deps.append(req.as_line())
        else:
            deps.append(req.as_line())
    data: dict[str, Any] = {}
    settings: dict[str, Any] = {}
    if dev_deps:
        dev_group = options.group if options.group and options.dev else "dev"
        settings["dev-dependencies"] = {dev_group: dev_deps}
    if options.group and deps:
        data["optional-dependencies"] = {options.group: deps}
    else:
        data["dependencies"] = deps
    sources: list[dict[str, Any]] = []
    if parser.index_url and not parser.no_index:
        sources.append(convert_url_to_source(parser.index_url, "pypi", parser.trusted_hosts))
    if not parser.no_index:
        for url in parser.extra_index_urls:
            sources.append(convert_url_to_source(url, None, parser.trusted_hosts))
    if parser.find_links:
        first, *find_links = parser.find_links
        sources.append(
            convert_url_to_source(
                first,
                "pypi" if parser.no_index else None,
                parser.trusted_hosts,
                "find_links",
            )
        )
        for url in find_links:
            sources.append(convert_url_to_source(url, None, parser.trusted_hosts, "find_links"))

    if sources:
        settings["source"] = sources
    return data, settings


def export(
    project: Project,
    candidates: list[Candidate] | list[Requirement],
    options: Namespace,
) -> str:
    from pdm.models.candidates import Candidate

    lines = ["# This file is @generated by PDM.\n# Please do not edit it manually.\n\n"]
    collected_req: set[str] = set()
    for candidate in sorted(candidates, key=lambda x: x.identify()):  # type: ignore[attr-defined]
        if isinstance(candidate, Candidate):
            req = candidate.req.as_pinned_version(candidate.version)
        else:
            assert isinstance(candidate, Requirement)
            req = candidate
        line = project.backend.expand_line(req.as_line(), options.expandvars)
        if line in collected_req:
            continue
        lines.append(project.backend.expand_line(req.as_line(), options.expandvars))
        collected_req.add(line)
        if options.hashes and getattr(candidate, "hashes", None):
            for item in sorted({row["hash"] for row in candidate.hashes}):  # type: ignore[attr-defined]
                lines.append(f" \\\n    --hash={item}")
        lines.append("\n")
    if (options.self or options.editable_self) and not project.is_distribution:
        raise PdmUsageError("Cannot export the project itself in a non-library project.")
    if options.self:
        lines.append(".  # this package\n")
    elif options.editable_self:
        lines.append("-e .  # this package\n")

    sources = project.pyproject.settings.get("source", [])
    for source in sources:
        url = source["url"]
        if options.expandvars:
            url = expand_env_vars_in_auth(url)
        source_type = source.get("type", "index")
        if source_type == "index":
            prefix = "--index-url" if source["name"] == "pypi" else "--extra-index-url"
        elif source_type == "find_links":
            prefix = "--find-links"
        else:
            raise ValueError(f"Unknown source type: {source_type}")
        lines.append(f"{prefix} {url}\n")
        if not source.get("verify_ssl", True):
            host = urllib.parse.urlparse(url).hostname
            lines.append(f"--trusted-host {host}\n")
    return "".join(lines)
