"""Paths of one study (one survey version), so that studies never mix.

    config/<study>/codebook.yaml (or the single codebook*.yaml there), run.yaml
    prompts/<study>/
    data/raw/<study>/responses_it.csv, responses_en.csv
    data/clean/<study>/
    data/photos/<study>/place1.jpg ...
    runs/<date-time>_<model>_<study>/

The code from here on follows the survey v2 codebook (pilot2 onwards). The smoke test
on survey v1 (pilot1) is reproducible from the git tag `smoke-test-v1`.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STUDY = "pilot2"
LANGS = ("it", "en")


class Study:
    def __init__(self, name=DEFAULT_STUDY):
        self.name = name
        self.config_dir = ROOT / "config" / name
        if not self.config_dir.is_dir():
            raise SystemExit(f"No study {name!r}: {self.config_dir.relative_to(ROOT)} does not exist")
        self.prompts = ROOT / "prompts" / name
        self.raw = ROOT / "data" / "raw" / name
        self.clean = ROOT / "data" / "clean" / name
        self.photos = ROOT / "data" / "photos" / name
        self.runs = ROOT / "runs"
        self.participants = self.clean / "participants.json"
        self.personas = self.clean / "personas.json"
        self.run_config = self.config_dir / "run.yaml"

    @property
    def codebook_path(self):
        exact = self.config_dir / "codebook.yaml"
        if exact.exists():
            return exact
        found = sorted(self.config_dir.glob("codebook*.yaml"))
        if len(found) != 1:
            raise SystemExit(f"Expected one codebook*.yaml in {self.config_dir.relative_to(ROOT)}, found {len(found)}")
        return found[0]

    def codebook(self):
        with open(self.codebook_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def raw_file(self, lang):
        return self.raw / f"responses_{lang}.csv"

    def photo(self, place):
        return self.photos / f"place{place}.jpg"


def add_study_arg(parser):
    parser.add_argument("--study", default=DEFAULT_STUDY,
                        help=f"study folder under config/ (default: {DEFAULT_STUDY})")
