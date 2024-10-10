import pandas as pd

class StatCollector(object):

    _instance = None
    stat_columns_backup = ['script name',
                    'code lines',
                    'nodes at start',
                    'total time',
                    'rules time',
                    'kb_lookup time',
                    'pipeline time',
                    'nodes at end']

    rule_columns = ['rule id',
                    'rule name',
                    'rule type',
                    'time',
                    'instances',
                    'wildcard amount',
                    'generalized instances',
                    'wildcard rules']

    stat_columns = ["nodes g1",
                    "nodes g2",
                    "nodes gdiff",
                    "nodes to add",
                    "nodes to delete",
                    "nodes to update",
                    #"edges to add",
                    #"edges to delete",
                    #"nodes in pattern",
                    #"time diff",
                    #"time rule creation",
                    #"time subgraph",
                    #"total time"
                    ]


    def __init__(self):
        #print('Initializing stat collector')
        self.scripts_df = pd.DataFrame(columns=StatCollector.stat_columns)
        self.rules_df = None
        self.rules_df_list = list()
        self.curr_rule_dataframe = pd.DataFrame()
        self.curr_script_data = {}
        self.curr_rule_data = {}
        self.collecting_data = False


    @staticmethod
    def getStatCollector():
        if StatCollector._instance == None:
            StatCollector._instance = StatCollector()
        return StatCollector._instance

    def get_current_stat_rules(self) -> pd.DataFrame:
        return self.curr_rule_dataframe

    def get_current_rule_data(self):
        return self.curr_rule_data

    def new_script(self):
        self.curr_rule_dataframe = pd.DataFrame()
        self.curr_script_data = {}
        self.rules_df_list = list()
        self.new_rule()
        self.scripts_df = pd.DataFrame(columns=StatCollector.stat_columns)

    def new_rule(self):
        self.curr_rule_data = {}

    def store_script_and_rule_dataframe(self):
        self.scripts_df = self.scripts_df.append(self.curr_script_data, ignore_index=True)

    def store_rule_dataframe(self):
        self.rules_df_list.append(self.curr_rule_dataframe)

    def store_rule_data(self):
        self.curr_rule_dataframe = self.curr_rule_dataframe.append(self.curr_rule_data, ignore_index=True)

    def append_script_data(self, data: dict):
        if not self.collecting_data:
            return
        self.curr_script_data.update(data)

    def append_rule_data(self, stats: dict):
        if not self.collecting_data:
            return
        self.curr_rule_data.update(stats)

    def export_script_data(self, name):
        self.scripts_df.to_csv(name, index=False, mode='a', header=False)
        pass

    def export_rule_data(self, name):
        self.rules_df = pd.concat(self.rules_df_list)
        self.rules_df.to_csv(name, index=False)

    def print(self):
        print(self.scripts_df.to_string())
        for index, stat_rule in enumerate(self.rules_df_list):
            print(f'Rule stats for script {index}:')
            print(stat_rule)