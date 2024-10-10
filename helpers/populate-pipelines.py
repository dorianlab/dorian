from pathlib import Path
import pandas as pd
import openml
import re
from dorian.collection.parser import parse
from dorian.languages import SupportedLanguage

openml.config.apikey = '024cb6eb1a204a0c93402df65cfd1588'


def make_pipeline(fid):
    flow = openml.flows.get_flow(fid)
    params = [(k, v['value'] if isinstance(v, dict) else v) for k, v in flow.parameters.items() if
              v and k not in ['steps', 'estimators', 'memory', 'verbose']]
    params = ", ".join(map(lambda t: f'{t[0]}={t[1]}', params)) if params else ''
    return flow.name + (f'({params})' if (params or flow.name[-1] != ')') else '')


def main():
    fpath = 'flows.csv'
    if Path(fpath).exists():
        flows = pd.read_csv(fpath)
    else:
        flows = openml.flows.list_flows(output_format='dataframe')
        flows = flows[flows.name.str.contains('sklearn') & (flows['version'].astype(int) == 1)]
        flows['pipeline'] = flows.id.apply(make_pipeline)
        s = flows.pipeline.str.len().sort_values().index
        flows.reindex(s).to_csv(fpath, index=False)
    print(flows)

    checked = 'checked_pipelines.csv'
    if Path(checked).exists():
        done = pd.read_csv(checked, names=['fid', 'tag'])['fid'].to_list()
        flows = flows[~flows.id.isin(done)]

    print(flows.shape[0])
    for idx, (fid, code) in flows.loc[:, ['id', 'pipeline']].iterrows():
        code = make_pipeline(fid)
        if re.findall(r'TEST|C37|C0x', code):
            print("***", fid, code)
            with open(checked, 'a') as f:
                f.write(f'{fid},s\n')
            continue
        print(fid, code)
        # print(dir(flow))
        # print(flow.components)
        # print(flow.dependencies)
        # print(flow.language)
        # print(flow.get_structure())
        # print(flow.model)
        # print(flow.version)
        dag = parse(code, language=SupportedLanguage.python)
        print(dag)
        r = True
        while r:
            r = input('[C]orrect, [W]rong, [S]kip, [E]dit, [Q]uit? ')
            match r.lower():
                case 'q':
                    return
                case 'c' | 'w' | 's' | 'e':
                    with open(checked, 'a') as f:
                        f.write(f'{fid},{r.lower()}\n')
                    r = False
                case other:
                    print(f'wrong tag {other}')
                    r = True
        print()
    return


if __name__ == "__main__":
    # import asyncio
    # loop = asyncio.new_event_loop()
    # loop.run_until_complete(main())
    main()
