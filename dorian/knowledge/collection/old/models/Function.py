class Function:
    class Argument:

        def __init__(self, name, type, position, default_value):
            self.name = name
            self.type = type
            self.position = position
            self.default_value = default_value

        def __str__(self):
            if self.type == "hyperparameter":
                return self.name + "=" + self.default_value
            else:
                return self.name

        def __repr__(self):
            return self.__str__()

    def __init__(self, name, description, link, args="", module_name="", language="", ds_task=""):
        self.module_name = module_name
        self.name = name
        self.description = description
        self.link = link
        self.language = language
        self.ds_task = ds_task
        if type(args) == str:
            args = Function.parse_args_from_string(args)
        self.args = args

    def __str__(self):
        return f'{self.name}, {self.description}, {self.link}, {self.language}\n args: {self.args}'

    @staticmethod
    def parse_from_list(input_list: list):
        # extract all attributes but arguments
        name, description, link = input_list[0], input_list[1], input_list[2]
        # extract arguments
        parsed_args = input_list[3:][0]

        if parsed_args == '':
            return Function(name, description, link, "")
        return Function(name, description, link, parsed_args)

    @staticmethod
    def parse_args_from_string(args_string: str):
        arguments = list()
        i = 0
        for arg in args_string.split(","):
            if "=" in arg:
                arg_name, arg_value = arg.split("=")[0], arg.split("=")[1]
                arguments.append(Function.Argument(arg_name, "hyperparameter", i + 1, arg_value))
            elif arg.count("*") == 1:
                arguments.append(Function.Argument(arg, "args", i + 1, None))
            elif arg.count("*") == 2:
                arguments.append(Function.Argument(arg, "kwargs", i + 1, None))
            else:
                arguments.append(Function.Argument(arg, "positional_argument", i + 1, None))
            i += 1
        return arguments

    @staticmethod
    def get_function_by_name_module_language(cursor, module_name, title, language):
        # check if this module exists
        cursor.execute(
            "SELECT module_name FROM modules WHERE module_name = ? and language = ?", (module_name, language)
        )
        name = cursor.fetchall()
        if name:
            # get function id to get its arguments
            cursor.execute(
                "SELECT function_id, module_name, function_title, description, link, language, data_science_task FROM functions WHERE module_name = ? AND function_title =? AND language =?",
                (module_name, title, language))
            func = cursor.fetchall()
            if func:
                func_id = func[0][0]
                # get arguments
                cursor.execute(
                    "SELECT argument_name, argument_type, argument_position, default_value FROM arguments WHERE function_id = ?",
                    (func_id,))
                args = cursor.fetchall()
                if len(args) == 0 and len(func) != 0:
                    #name, description, link, args, module_name="", language="", ds_task=""
                    return Function(func[0][2], func[0][3], func[0][4], module_name=func[0][1], language=func[0][5], ds_task=func[0][6])
                elif len(func) != 0:
                    arguments = []
                    for arg in args:
                        if arg[1] != "hyperparameter":
                            arguments.append(str({"argument_name": arg[0], "argument_type": arg[1], "default_value": arg[3]}))
                        else:
                            arguments.append(str({"argument_name": arg[0], "argument_type": arg[1], "argument_position": arg[2]}))
                if len(func) != 0:
                    # name, description, link, args, module_name="", language="", ds_task=""
                    function = Function(func[0][2], func[0][3], func[0][4], args=args, module_name=func[0][1], language=func[0][5], ds_task=func[0][6])
                    return function
        return None

    @staticmethod
    def parse_from_db_by_name_and_language(cursor, title, language):
        cursor.execute(
            "SELECT function_id FROM functions WHERE function_title =? AND language =?",
            (title, language))
        func = cursor.fetchall()
        return func

    def insert_function(self):
        pass


if __name__ == "__main__":
    f = ['pandas.read_pickle', 'Load pickled pandas object (or any object) from file.',
         'https://pandas.pydata.org/docs/reference/api/pandas.read_pickle.html',
         "filepath_or_buffer,compression='infer',storage_options=None"]
    s = "pandas.HDFStore.append,Append to Table in file.,https://pandas.pydata.org/docs/reference/api/pandas.HDFStore.append.html,\"key,value,format=None,axes=None,index=True,append=True,complib=None,complevel=None,columns=None,min_itemsize=None,nan_rep=None,chunksize=None,expectedrows=None,dropna=None,data_columns=None,encoding=None,errors='strict'\""
    function = Function.parse_from_list(f)
    print(function)
