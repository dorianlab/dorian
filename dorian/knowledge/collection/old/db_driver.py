import datetime
import sqlite3
import csv
import os
from models.Function import Function
import json
import yaml
from utils import read_rule_from_string


def init_tables(cursor, connection):
    """
    Initializes database.

    Creates empty tables "scraped_modules", "function", "arguments", "rules".
    :param cursor: connection cursor
    """
    cursor.execute("DROP TABLE IF EXISTS functions")
    cursor.execute("DROP TABLE IF EXISTS arguments")
    cursor.execute("DROP TABLE IF EXISTS modules")
    cursor.execute("DROP TABLE IF EXISTS rules")
    cursor.execute(
        "CREATE TABLE modules("
        "module_id INTEGER NOT NULL, "
        "module_name, "
        "version, "
        "date, "
        "language,"
        "PRIMARY KEY (module_name, language))")
    cursor.execute(
        "CREATE TABLE functions("
        "function_id INTEGER PRIMARY KEY, "
        "module_name, "
        "function_title type UNIQUE, "
        "description, "
        "link, "
        "language, "
        "data_science_task, "
        "FOREIGN KEY(module_name, language) REFERENCES scraped_modules(module_name, language))")
    cursor.execute(
        "CREATE TABLE arguments("
        "function_id, "
        "argument_name, "
        "argument_type, "
        "argument_position, "
        "default_value, "
        "FOREIGN KEY(function_id) REFERENCES functions(function_id))")
    cursor.execute(
        "CREATE TABLE rules("
        "rule_id, "
        "rule_name PRIMARY KEY, "
        "rule_description, "
        "rule, rule_type, "
        "added_by_user, "
        "language, "
        "priority)")
    connection.commit()


def init_module_from_file(module_name, version, date, language, cursor, connection, filename="", file=None):
    with open(filename, newline='') as csvfile:
        csvreader = csv.reader(csvfile)
        header = next(csvreader)
        cursor.execute(
            "SELECT COUNT(*) "
            "FROM modules")
        module_id = cursor.fetchall()[0][0] + 1
        # add module info
        cursor.execute(
            "INSERT INTO modules(module_id, module_name, version, language, date) "
            "VALUES(?, ?, ?, ?, ?)",
            [module_id, module_name, version, language, date])
        for (index, row) in enumerate(csvreader):
            function = Function.parse_from_list(row)
            # add function values
            cursor.execute(
                "INSERT INTO functions(module_name, function_title, description, link, language) "
                "VALUES(?, ?, ?, ?, ?)",
                [module_name, function.name, function.description, function.link, language])
            added_function_id = cursor.lastrowid
            if function.args is not None:
                for arg in function.args:
                    cursor.execute(
                        "INSERT INTO arguments "
                        "VALUES(?, ?, ?, ?, ?)",
                        [added_function_id, arg.name, arg.type, arg.position, arg.default_value])
    connection.commit()
    return f"Module {module_name} has been added to the knowledge base."


def init_ds_tasks(language, cursor):
    """
    Parses data science ontology library into a dictionary and
    dumps it into a json file.
    """
    ds_tasks = {}

    # find all yaml files
    yaml_files = []

    for path, subdirs, files in os.walk("knowledge_base/ds_annotation/" + language):
        for name in files:
            file_path = os.path.join(path, name)
            yaml_files.append(file_path)

    for file in yaml_files:
        with open(file) as stream:
            data_loaded = yaml.safe_load(stream)
            if type(data_loaded["definition"]) is list:
                continue
            if "class" in data_loaded.keys():
                if type(data_loaded["class"]) is list:
                    for class_name in data_loaded["class"]:
                        ds_tasks[class_name] = data_loaded["definition"]
                else:
                    ds_tasks[data_loaded["class"]] = data_loaded["definition"]
            elif "function" in data_loaded.keys():
                ds_tasks[data_loaded["function"]] = data_loaded["definition"]

    # save result into json
    json_object = json.dumps(ds_tasks, indent=2)
    # with open("knowledge_base/ds_tasks/" + language +"/ds_tasks.json", "w") as outfile:
    # outfile.write(json_object)


def assign_ds_tasks(cursor, connection):
    # assign data science task to a respective function from knowledge base
    task_dirs = [f.name for f in os.scandir("data_science_pipelines/knowledge_base/ds_tasks/") if f.is_dir()]
    for language in task_dirs:
        f = open("data_science_pipelines/knowledge_base/ds_tasks/" + language + "/ds_tasks.json")
        data = json.load(f)
        for key in data:
            function = Function.parse_from_db_by_name_and_language(cursor, key, language)
            if len(function) != 0:
                function_id = function[0][0]
                cursor.execute(
                    "UPDATE functions SET data_science_task = ? WHERE function_id = ?", (data[key], function_id))
        connection.commit()


def init_rules_from_file(cursor, connection):
    cursor.execute(
        "SELECT COUNT(*) "
        "FROM rules")
    added_rule_id = cursor.fetchall()[0][0] + 1
    with open("data_science_pipelines/knowledge_base/rules/rule_base.txt") as file:
        for counter, line in enumerate(file, 0):
            json_rule = read_rule_from_string(line)
            if "name" in json_rule:
                rule_name = json_rule.pop("name")
            if "description" in json_rule:
                rule_desc = json_rule.pop("description")
            if "type" in json_rule:
                rule_type = json_rule.pop("rule_type")
            else:
                rule_type = "syntactic"
            if "language" in json_rule:
                rule_language = json_rule.pop("language")
            if "priority" in json_rule:
                rule_priority = json_rule.pop("priority")
            added_by_user = False
            cursor.execute(
                "INSERT INTO rules(rule_id, rule_name, rule_description, rule, rule_type, added_by_user, language, priority) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                [added_rule_id, rule_name, rule_desc, str(json_rule), rule_type, added_by_user, rule_language,
                 rule_priority])
            added_rule_id = cursor.lastrowid + 1
    connection.commit()
    pass


def reset_db():
    # establish connection
    connection = sqlite3.connect("knowledge_base.db")
    cursor = connection.cursor()

    # init database
    init_tables(cursor, connection)
    #print(os.getcwdb())
    # init modules and functions
    folders = os.listdir("data_science_pipelines/knowledge_base/scraped_modules/")
    for language in folders:
        files = os.listdir("data_science_pipelines/knowledge_base/scraped_modules/" + language)
        for file in files:
            module_name, date, version = file.replace(".csv", "").split(" ")
            date_format = "%Y-%m-%d"
            date = datetime.datetime.strptime(date, date_format).date()
            module_path = "data_science_pipelines/knowledge_base/scraped_modules/" + language + "/" + file
            init_module_from_file(module_name, version, date, language, cursor, connection, filename=module_path)

    # init data science tasks
    ds_folders = [f.name for f in os.scandir("data_science_pipelines/knowledge_base/ds_annotation/") if f.is_dir()]
    for language in ds_folders:
        init_ds_tasks(language, cursor)
    assign_ds_tasks(cursor, connection)

    # init rules
    init_rules_from_file(cursor, connection)

    connection.close()
    return "Database initialized successfully"


if __name__ == "__main__":
    reset_db()
