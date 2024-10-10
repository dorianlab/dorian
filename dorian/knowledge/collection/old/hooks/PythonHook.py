from hooks.LanguageHook import LanguageHook
from rule_executioner import find_matching_optimised
import utils
from regraph import NXGraph, Rule


class PythonHook (LanguageHook):
    def __init__(self):
        self.aliases_dict = {}
        self.functions_dict = {}
        self.imported_modules = []

    def print(self):
        print("ik bin de snake hook")

    def pre_hooks(self, G: NXGraph):
        self.aliases_dict = self.save_import_aliases(G)
        self.functions_dict = self.save_imported_functions(G)
        self.imported_modules = self.save_imported_modules(G)
        self.remove_import_statements(G)
        return G

    def post_hooks(self, G: NXGraph):
        # maybe keep a list of nodes with full names here
        mapping = {"module_node_attr": "module", "full_function_name_attribute": "full_function_name"}
        # append new ones after each function
        self.add_attributes_from_import_aliases(G, self.aliases_dict)
        self.add_attributes_from_functions_dict(G, self.functions_dict)
        self.add_attributes_from_module_list(G, self.imported_modules)
        # when no module name is present, append "built-in"
        #self.add_full_names_to_nodes_without_modules(G)
        # return node name where module name and full function name are stored
        return G, mapping


    def remove_import_statements(self, G: NXGraph):
        """
        Removes import statements from a given NXGraph object.
        It searches for both 'import_statement' and 'import_from_statement' nodes
        and removes them along with their ancestors nodes.

        Parameters:
        G (NXGraph): The NXGraph object containing the import statements to remove.

        Returns:
        None
        """
        instances = []
        pattern = NXGraph()
        pattern.add_node(1, {'type': 'import_statement'})
        instances.extend(find_matching_optimised(G, pattern))
        #instances.extend(find_matching(G, pattern))

        pattern = NXGraph()
        pattern.add_node(1, {'type': 'import_from_statement'})
        instances.extend(find_matching_optimised(G, pattern))
        #instances.extend(find_matching(G, pattern))
        if instances:
            for instance in instances:
                nodes = utils.get_ancestors_nodes(G, instance[1])
                for node in nodes:
                    G.remove_node(node)
        return

    def save_import_aliases(self, G: NXGraph):
        """
        Handles 'import numpy as np'.

        :param G: A NXGraph object
        """
        # create pattern for aliases
        pattern = NXGraph()
        pattern.add_node(1, {'type': 'aliased_import'})
        pattern.add_node(2, {'type': 'dotted_name'})
        pattern.add_node(3, {'type': 'identifier'})
        pattern.add_edge(2, 1)
        pattern.add_edge(3, 1)

        instances = find_matching_optimised(G, pattern)
        #instances = find_matching(G, pattern)

        aliases_dict = {}

        # save all aliases
        for instance in instances:
            aliased_import_id = instance[1]
            dotted_name_id = instance[2]
            identifier_id = instance[3]

            dotted_name = G.get_node(dotted_name_id)
            identifier = G.get_node(identifier_id)
            for key, value in zip(identifier["text"], dotted_name["text"]):
                aliases_dict[key] = value

        for alias, module in aliases_dict.items():
            nodes_to_remove = [x for x in G.nodes(True) if next(iter(x[1]['text'])) == alias]
            for node in nodes_to_remove:
                G.remove_node(node[0])
        return aliases_dict

    def save_imported_functions(self, G: NXGraph):
        """
        Handles "from numpy import function".

        :param G:
        :return:
        """
        pattern = NXGraph()
        pattern.add_node(1, {'type': 'dotted_name'})
        pattern.add_node(2, {'type': 'import_from_statement'})
        pattern.add_node(3, {'type': 'dotted_name'})
        pattern.add_edge(1, 2)
        pattern.add_edge(3, 2)

        instances = find_matching_optimised(G, pattern)
        #instances = find_matching(G, pattern)

        functions_dict = {}
        if instances:
            for instance in instances:
                if instance[1] < instance[3]:
                    module_name = G.get_node(instance[1])["text"]
                    function_name = G.get_node(instance[3])["text"]
                    for mod_name, func_name in zip(module_name, function_name):
                        functions_dict[func_name] = mod_name + "." + func_name
                        # print(functions_dict)
        return functions_dict

    def save_imported_modules(self, G: NXGraph):
        """
        Handles "import module".

        Finds instances of imported scraped_modules "dotted_name" -> "import_statement"
        and returns them as a list of strings.

        :param G: A NXGraph object
        :return: A list of strings representing the imported scraped_modules
        """

        pattern = NXGraph()
        pattern.add_node(1, {'type': 'dotted_name'})
        pattern.add_node(2, {'type': 'import_statement'})
        pattern.add_edge(1, 2)
        instances = []

        instances = find_matching_optimised(G, pattern)
        #instances = find_matching(G, pattern)

        imported_modules = []
        if instances:
            for instance in instances:
                imported_modules.append(G.get_node(instance[1])["text"])

        return imported_modules

    def add_attributes_from_import_aliases(self, G: NXGraph, aliases_dict: dict):
        """
        :param G: a NXGraph object
        :param alieses_dict: A dictionary of import aliases, where keys are the short identifiers
         and values are the full function names.
        :param cursor: A cursor object to access a database, which is used to retrieve a
        knowledge base entry for the functions
        """
        for alias, module in aliases_dict.items():
            nodes_to_change = [x for x in G.nodes(True) if next(iter(x[1]['text'])).startswith(alias + '.')]
            for node in nodes_to_change:
                G.update_node_attrs(node[0], {"text": next(iter(node[1]['text'])).replace(alias, module, 1),
                                              "type": node[1]["type"]})
                G.add_node_attrs(node[0], {"full_function_call": next(iter(node[1]["text"])).partition("(")[0]})
                G.add_node_attrs(node[0], {"module": module})
                G.add_node_attrs(node[0], {"alias": alias})

                # get knowledge base entry
                # kb_function = Function.get_function_by_name_module_language(cursor, module_name, full_name)
                # if kb_function != -1:
                #    G.add_node_attrs(node_id, {"description": kb_function.description})

    def add_attributes_from_functions_dict(self, G: NXGraph, functions_dict: dict):
        """
        from numpy import function
        """
        for key in functions_dict:
            pattern = NXGraph()
            pattern.add_node(1, {'text': key})
            instances = find_matching_optimised(G, pattern)
            for instance in instances:
                # add "module" attribute and put full function name there
                node_id = instance[1]
                index = functions_dict[key].find(".")
                module_name = functions_dict[key][:index]
                G.add_node_attrs(node_id, {"module": module_name})
                # add full function call to the node
                full_name = functions_dict[key]
                G.add_node_attrs(node_id, {"full_function_call": full_name})
                # get knowledge base entry
                # kb_function = Function.parse_from_db_by_name_and_module(cursor, module_name, full_name)
                # if kb_function != -1:
                #    G.add_node_attrs(node_id, {"description": kb_function.description})

    def add_attributes_from_module_list(self, G: NXGraph, imported_modules: list):
        for module in imported_modules:
            for module_name in module:
                pattern = NXGraph()
                pattern.add_node(1, {'identifier': module_name})
                instances = find_matching_optimised(G, pattern)
                for instance in instances:
                    # add "module" attribute and put full function name there
                    node_id = instance[1]
                    function_call = G.get_node(node_id)["text"]
                    for function_name in function_call:
                        G.add_node_attrs(node_id, {"module": module_name})
                        # add full function call
                        G.add_node_attrs(node_id, {"full_function_call": function_name})
                        # kb_function = Function.parse_from_db_by_name_and_module(cursor, module_name.decode("utf-8"), function_name.decode("utf-8"))
                        # if kb_function != -1:
                        #    G.add_node_attrs(node_id, {"description": kb_function.description})

    def add_full_names_to_nodes_without_modules(self, G):
        # search for all calls or attributes
        for node, attrs in G.nodes(True):
            if (next(iter(attrs["type"])) == "call" or next(
                    iter(attrs["type"])) == "attribute") and "full_function_call" not in attrs.keys():
                new_node_attrs = attrs.copy()
                #new_node_attrs["full_function_name"] = attrs["text"]
                #new_node_attrs["module"] = "built-in"
                G.update_node_attrs(node, new_node_attrs)
        return G

def get_hook() -> LanguageHook:
    return PythonHook()
