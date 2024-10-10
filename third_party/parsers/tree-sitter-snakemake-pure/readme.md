This tree-sitter-snakemake works now, but it only works for snakemake grammar without original python grammar.


In examples, you can parse “simple_test.snakefile” , “test1_multiple_rules.snakefile”, "test_multiple_input.snakefile" and "test_multiple_rules_input.snakefile" successfully.

But for “test_with_python1.snakefile” and “test_with_python2.snakefile” with python expression in it , the parser doesn’t work.


remaining issues:
1.number(integer) can not appear in the "string"
2.although "input","output"....can recognize multiple lines,"shell" can only recognize single line, because there is no "," as a connection between two values of "shell"
3.no "statement" 
