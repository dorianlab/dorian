
# Dorian
Dorian PoC

# Setup

Install [pyenv](https://github.com/pyenv/pyenv), [poetry](https://python-poetry.org/), and [nvm](https://github.com/nvm-sh/nvm) for management of the development environment. In the `pyproject.toml`, see line `python = ...` for the acceptable Python version, e.g., `"^3.11"`, and do the following from the root directory of the project 
```
pyenv install <python version>
pyenv local <python version>
poetry env use <python version>
poetry install
```
e.g., `<python version>` is 3.11.9.

For missing packages, use `poetry add <package name>` (see [poetry docs](https://python-poetry.org/)).

To run the backend, use `poetry run uvicorn main:app --reload`
To run the frontend, use 

```bash
cd frontend/
npm install
npm run dev
```

Note that, for development, both processes need to be executed simultaneously.
