"""
dorian.vault
------------
Encrypted user environment variable management.

Users store API keys and secrets encrypted with a client-side passphrase.
The server holds only ciphertext and can decrypt only when the user
provides their passphrase at pipeline execution time.
"""
