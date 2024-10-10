import json

import test_scripts
from evaluation.stat_collector import StatCollector
from hooks.PythonHook import PythonHook
from dsp_api import DSP_API
import utils
from evaluation.scripts import scripts

def test_graph_extractor(api):
    code = test_scripts.Python.code_0
    language = 'python'
    hook = PythonHook()
    write_to_file = True
    output_path = "evaluation/outputs/ds_pipeline.json"
    api.create_pipeline(code, language, hook, write_to_file, output_path)


def test_extract_rule(api):
    f1 = open('evaluation/g1.json')
    g1 = json.load(f1)
    f2 = open("evaluation/g2.json")
    g2 = json.load(f2)
    G1 = utils.json_to_nxgraph_for_gdiff(g1)
    G2 = utils.json_to_nxgraph_for_gdiff(g2)

    pattern, result = api.extract_rule(G1, G2, "semantic")
    print(pattern)
    print(result)
    return pattern, result


def test_confirm_rule(api, pattern, result):
    rule = api.confirm_rule(pattern, result, "testname", "testdescr", "python", "semantic", 50)
    print(rule)


def test_list_rules(api):
    print(api.list_rules("python"))
    pass


def test_delete_rule(api):
    print(api.delete_rule("testname"))
    pass


def test_add_rule(api):
    f = open("knowledge_base/rules/rule_creation.json", "r")
    rule_dict = json.loads(f.read())
    api.add_rule(rule_dict)
    pass


def test_add_module(api):
    print(api.add_module("test_module", "1.2.3", "2023-12-15", "python"))
    pass


def test_add_function(api):
    print(api.add_function("test_module", "test_func", description="1234", link="345", language="python",
                           data_science_task="fewrg", args="e=1"))


def test_add_ds_task(api):
    print(api.add_ds_task("test_module", "test_func", "python", "test_task"))


def evaluate_script_collection(api):
    language = 'python'
    hook = PythonHook()
    write_to_file = True
    stat = StatCollector.getStatCollector()
    stat.collecting_data = True

    stat.new_script()
    # stat.append_script_data({'script name': scr})
    # api.create_pipeline(scripts[scr]["script"], language, hook, write_to_file)

    for scr in scripts:
        #if scr not in ["short5"]:
            #continue
        if scr not in ["short1", "short2", "short4", "short5", "short6", "short7", "short10", "sklearn1", "sklearn2",
                       "sklearn3", "sklearn4", "sklearn5", "sklearn6", "sklearn7", "sklearn9", "sklearn10", "sklearn12",
                       "kaggle7", "kaggle9", "kaggle12"]:
            continue
        for i in range(1):
            #print(scr)
            #stat.new_script()
            #stat.append_script_data({'script name': scr})
            api.create_pipeline(scripts[scr]["script"], language, hook, write_to_file, output_path="outputs/"+scr+".json")

            #stat.store_script_and_rule_dataframe()
            #stat.export_script_data("evaluation/results/data/graph_diff_data/" + scr + ".csv")
    #stat.print()


def eval_graph_diff(api):
    test_extract_rule(api)

if __name__ == '__main__':
    api = DSP_API()
    # test_graph_extractor(api)
    # pattern, result = test_extract_rule(api)
    # test_confirm_rule(api, pattern, result)
    # test_list_rules(api)
    # test_delete_rule(api)
    # test_add_rule(api)
    # test_add_module(api)
    # test_add_function(api)
    # test_add_ds_task(api)
    #evaluate_script_collection(api)
    #eval_graph_diff(api)
