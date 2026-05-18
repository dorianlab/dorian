from pathlib import Path
import shutil


if __name__ == "__main__":
    for pattern in ['__pycache__', 'node_modules']:
        for f in Path(__file__).parent.glob(f"**/{pattern}"):
            shutil.rmtree(f)
            print(f.as_posix())
