from typing import Literal

SupportedLanguage = Literal['python']

# Convenience constants for rule/pattern definitions.
# ``SupportedLanguage`` is a ``Literal`` type alias (used for type-checking),
# so ``SupportedLanguage.python`` no longer works.  Import ``PYTHON`` instead.
PYTHON: SupportedLanguage = "python"  # type: ignore[assignment]