from pathlib import Path
import json
import re

patterns = {
    'Cannot extract more clusters than samples': r'ValueError: Cannot extract more clusters than samples: (\d+) clusters were given for a tree with (\d+) leaves.',
    'Could not convert string to float': r'ValueError: could not convert string to float: \'([\w\s-]+)\'',
    'Operator does not accept missing values encoded as NaN natively.': r'(\w+) does not accept missing values encoded as NaN natively.*',
    'Invalid literal for int()': r'ValueError: invalid literal for int\(\) with base 10: \'([\w\s-]+)\'',
    'Floating-point under-/overflow': r'ValueError: Floating-point under-/overflow occurred at epoch #(\d+)\. Scaling input data with StandardScaler or MinMaxScaler might help\.',
}

msgs = []
for last in sorted(Path('/scratch/dorian/experiments/checkpoints').glob('**/result.json')):
    with open(last, 'r') as f:
        for line in f:
            result = json.loads(line)
            if result['message']:
                msg = result['message'].split('\n')[-2]
                matched = False
                for k, v in patterns.items():
                    if re.match(v, msg):
                        msgs.append(k)
                        matched = True
                        break
                if not matched:
                    msgs.append(msg)

for m in sorted(set(msgs)):
    print(m)

