from tree_sitter import Language, Parser
from pathlib import Path

base = Path(__file__).parents[1]

Language.build_library(
    # Store the library in the `build` directory
    str(base / 'lib/dorian-languages.so'),
    # Include one or more languages
    [
        str(base / 'third_party/parsers/tree-sitter-python'),
        str(base / 'third_party/parsers/tree-sitter-r'),
        str(base / 'third_party/parsers/tree-sitter-snakemake-pure')
    ]
)