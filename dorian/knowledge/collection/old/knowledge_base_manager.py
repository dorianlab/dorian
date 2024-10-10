import sqlite3
from models.Function import Function
from db_driver import init_module_from_file

class KnowledgeBaseManager:

    def __init__(self):
        self.connection = sqlite3.connect("knowledge_base.db")
        self.cursor = self.connection.cursor()

    def add_module_name_to_kb(self, name, version, date, language):
        # check if module in db
        self.cursor.execute("SELECT * FROM modules WHERE module_name = ? AND language = ?", (name, language))
        module_list = self.cursor.fetchall()
        if len(module_list) != 0:
            return "This module + language combination already exists!"
        # otherwise
        self.cursor.execute(
            "SELECT COUNT(*) "
            "FROM modules")
        module_id = self.cursor.fetchall()[0][0] + 1
        # add module info
        self.cursor.execute(
            "INSERT INTO modules(module_id, module_name, version, language, date) "
            "VALUES(?, ?, ?, ?, ?)",
            [module_id, name, version, language, date])
        self.connection.commit()
        return f"Module {name} has been created."

    def add_module_to_kb(self, module_name, version, date, language, file):
        return init_module_from_file(module_name, version, date, language, self.cursor, self.connection, file=file, filename="")

    def add_function_to_kb(self, module_name, function_title, description="", link="", language="", data_science_task="", args=""):
        self.cursor.execute("SELECT * FROM modules WHERE module_name=? and language = ?", (module_name, language))
        module_present = self.cursor.fetchall()
        if len(module_present) == 0:
            return "This module doesn't exist yet!"
        function = Function(function_title, description, link, args, module_name=module_name, language=language, ds_task=data_science_task)

        self.cursor.execute(
            "INSERT INTO functions(module_name, function_title, description, link, language, data_science_task) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            [function.module_name, function.name, function.description, function.link, function.language, function.ds_task])
        added_function_id = self.cursor.lastrowid
        if function.args is not None:
            for arg in function.args:
                self.cursor.execute(
                    "INSERT INTO arguments "
                    "VALUES(?, ?, ?, ?, ?)",
                    [added_function_id, arg.name, arg.type, arg.position, arg.default_value])
        self.connection.commit()
        return function

    def add_ds_task_to_kb(self, module_name, function_name, language, ds_task):
        self.cursor.execute("SELECT * FROM functions WHERE function_title=? and module_name = ? and language=?", (function_name, module_name, language))
        function_exists = self.cursor.fetchall()
        if len(function_exists) == 0:
            return "This function doesn't exist!"
        self.cursor.execute(
            "UPDATE functions SET data_science_task = ? WHERE function_title=? and module_name = ? and language=?", (ds_task, function_name, module_name, language))
        self.connection.commit()
        print(f"Data science task {ds_task} successfully added to function {function_name}.")
        return f"Data science task {ds_task} was added to function {function_name}."

    def add_description(self, module_name, function_name, language, description):
        self.cursor.execute("SELECT * FROM functions WHERE function_title=? and module_name = ? and language=?", (function_name, module_name, language))
        function_exists = self.cursor.fetchall()
        if len(function_exists) == 0:
            return "This function doesn't exist!"
        self.cursor.execute(
            "UPDATE functions SET description = ? WHERE function_title=? and module_name = ? and language=?", (description, function_name, module_name, language))
        self.connection.commit()
        print(f"Description {description} successfully added to function {function_name}.")
        return f"Description {description} successfully added to function {function_name}."


if __name__ == "__main__":
    mgr = KnowledgeBaseManager()
    mgr.add_ds_task_to_kb("built-in", "print", "python", "user_output")
    pass
