import json
import utils
from hooks.LanguageHook import LanguageHook
from rule_extractor import RuleExtractor
import db_driver
from graph_extractor import GraphExtractor
from rule_manager import RuleManager
from knowledge_base_manager import KnowledgeBaseManager


class DSP_API():

    def __init__(self):
        self.rule_extractor = RuleExtractor()
        self.graph_extractor = GraphExtractor()
        # self connection to db?
        self.rule_manager = RuleManager()
        self.kb_manager = KnowledgeBaseManager()

    def init_db(self):
        return db_driver.reset_db()

    def create_pipeline(self, code, language, hook: LanguageHook = None, write_to_file=True,
                        output_path="outputs/ds_pipeline.json"):
        graph = self.graph_extractor.extract_pipeline(code, language, hook)
        # print(graph)
        if write_to_file:
            out_file = open(output_path, "w")
            json.dump(graph, out_file, indent=4)
            out_file.close()
            # return graph
        else:
            return graph

    def extract_rule(self, g1, g2, rule_type):
        return self.rule_extractor.extract_rule(g1, g2, rule_type)

    def confirm_rule(self, pattern, result, rule_name, rule_description, language, rule_type="semantic", priority=50):
        pattern_g = utils.json_to_nxgraph(pattern)
        result_g = utils.json_to_nxgraph(result)
        return self.rule_extractor.adapt_rule(pattern_g, result_g, rule_name, rule_description, language, rule_type,
                                              priority)

    # how to return the manager correctly so that it is not created every time?
    def list_rules(self, language):
        return self.rule_manager.list_all_rules(language)

    def visualize_rule(self, rule_name):
        extractor = RuleExtractor()
        self.rule_manager.visualize_rule(rule_name, extractor)
        pass

    def delete_rule(self, rule_name):
        return self.rule_manager.delete_rule_by_name(rule_name)

    def add_PaT_rule(self, rule_dict):
        return self.rule_manager.add_rule_to_db(rule_dict)

    # todo implement
    def add_regraph_rule(self, rule_dict):
        return

    def add_module_name(self, name, version, date, language):
        return self.kb_manager.add_module_name_to_kb(name, version, date, language)

    def add_module(self, module_name, version, date, language, file):
        return self.kb_manager.add_module_to_kb(module_name, version, date, language, file=file)

    def add_function(self, module_name, function_title, description="", link="", language="", data_science_task="",
                     args=""):
        return self.kb_manager.add_function_to_kb(module_name, function_title, description, link, language,
                                                  data_science_task, args)

    def add_ds_task(self, module_name, function_name, language, ds_task):
        return self.kb_manager.add_ds_task_to_kb(module_name, function_name, language, ds_task)

    def add_description(self, module_name, function_name, language, description):
        return self.kb_manager.add_description(module_name, function_name, language, description)
