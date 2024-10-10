#include <tree_sitter/parser.h>

#if defined(__GNUC__) || defined(__clang__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wmissing-field-initializers"
#endif

#define LANGUAGE_VERSION 13
#define STATE_COUNT 92
#define LARGE_STATE_COUNT 2
#define SYMBOL_COUNT 75
#define ALIAS_COUNT 1
#define TOKEN_COUNT 45
#define EXTERNAL_TOKEN_COUNT 6
#define FIELD_COUNT 6
#define MAX_ALIAS_SEQUENCE_LENGTH 4
#define PRODUCTION_ID_COUNT 14

enum {
  anon_sym_rule = 1,
  anon_sym_COLON = 2,
  anon_sym_include_COLON = 3,
  anon_sym_workdir_COLON = 4,
  anon_sym_module = 5,
  anon_sym_configfile = 6,
  anon_sym_use = 7,
  anon_sym_STAR = 8,
  anon_sym_from = 9,
  anon_sym_as = 10,
  anon_sym_with = 11,
  anon_sym_input = 12,
  anon_sym_COMMA = 13,
  anon_sym_output = 14,
  anon_sym_params = 15,
  anon_sym_log = 16,
  anon_sym_cache = 17,
  anon_sym_message = 18,
  anon_sym_threads = 19,
  anon_sym_resources = 20,
  anon_sym_conda = 21,
  anon_sym_container = 22,
  anon_sym_shell = 23,
  anon_sym_script = 24,
  anon_sym_notebook = 25,
  anon_sym_snakefile = 26,
  anon_sym_meta_wrapper = 27,
  anon_sym_config = 28,
  anon_sym_skip_validation = 29,
  sym_comment = 30,
  sym_identifier = 31,
  anon_sym_LBRACE_LBRACE = 32,
  anon_sym_RBRACE_RBRACE = 33,
  sym_escape_sequence = 34,
  sym__not_escape_sequence = 35,
  anon_sym_True = 36,
  anon_sym_False = 37,
  sym_integer = 38,
  sym__newline = 39,
  sym__indent = 40,
  sym__dedent = 41,
  sym__string_start = 42,
  sym__string_content = 43,
  sym__string_end = 44,
  sym_snakemake = 45,
  sym_rule = 46,
  sym__include = 47,
  sym__workdir = 48,
  sym__configfile = 49,
  sym__ni = 50,
  aux_sym__norunparams = 51,
  sym__ruleparams = 52,
  sym_input = 53,
  sym_output = 54,
  sym__params = 55,
  sym_log = 56,
  sym_cache = 57,
  sym_message = 58,
  sym_threads = 59,
  sym__resources = 60,
  sym_conda = 61,
  sym_container = 62,
  sym_shell = 63,
  sym__shellparams = 64,
  sym_script = 65,
  sym_notebook = 66,
  sym__parameter_list = 67,
  sym_string = 68,
  sym__escape_interpolation = 69,
  sym_boolean = 70,
  aux_sym_snakemake_repeat1 = 71,
  aux_sym_input_repeat1 = 72,
  aux_sym__shellparams_repeat1 = 73,
  aux_sym_string_repeat1 = 74,
  anon_alias_sym_content = 75,
};

static const char * const ts_symbol_names[] = {
  [ts_builtin_sym_end] = "end",
  [anon_sym_rule] = "rule",
  [anon_sym_COLON] = ":",
  [anon_sym_include_COLON] = "include:",
  [anon_sym_workdir_COLON] = "workdir:",
  [anon_sym_module] = "module",
  [anon_sym_configfile] = "configfile",
  [anon_sym_use] = "use",
  [anon_sym_STAR] = "*",
  [anon_sym_from] = "from",
  [anon_sym_as] = "as",
  [anon_sym_with] = "with",
  [anon_sym_input] = "input",
  [anon_sym_COMMA] = ",",
  [anon_sym_output] = "output",
  [anon_sym_params] = "params",
  [anon_sym_log] = "log",
  [anon_sym_cache] = "cache",
  [anon_sym_message] = "message",
  [anon_sym_threads] = "threads",
  [anon_sym_resources] = "resources",
  [anon_sym_conda] = "conda",
  [anon_sym_container] = "container",
  [anon_sym_shell] = "shell",
  [anon_sym_script] = "script",
  [anon_sym_notebook] = "notebook",
  [anon_sym_snakefile] = "snakefile",
  [anon_sym_meta_wrapper] = "meta_wrapper",
  [anon_sym_config] = "config",
  [anon_sym_skip_validation] = "skip_validation",
  [sym_comment] = "comment",
  [sym_identifier] = "identifier",
  [anon_sym_LBRACE_LBRACE] = "{{",
  [anon_sym_RBRACE_RBRACE] = "}}",
  [sym_escape_sequence] = "escape_sequence",
  [sym__not_escape_sequence] = "_not_escape_sequence",
  [anon_sym_True] = "True",
  [anon_sym_False] = "False",
  [sym_integer] = "integer",
  [sym__newline] = "_newline",
  [sym__indent] = "_indent",
  [sym__dedent] = "_dedent",
  [sym__string_start] = "\"",
  [sym__string_content] = "_string_content",
  [sym__string_end] = "\"",
  [sym_snakemake] = "snakemake",
  [sym_rule] = "rule",
  [sym__include] = "_include",
  [sym__workdir] = "_workdir",
  [sym__configfile] = "_configfile",
  [sym__ni] = "_ni",
  [aux_sym__norunparams] = "_norunparams",
  [sym__ruleparams] = "_ruleparams",
  [sym_input] = "input",
  [sym_output] = "output",
  [sym__params] = "_params",
  [sym_log] = "log",
  [sym_cache] = "cache",
  [sym_message] = "message",
  [sym_threads] = "threads",
  [sym__resources] = "_resources",
  [sym_conda] = "conda",
  [sym_container] = "container",
  [sym_shell] = "shell",
  [sym__shellparams] = "_shellparams",
  [sym_script] = "script",
  [sym_notebook] = "notebook",
  [sym__parameter_list] = "_parameter_list",
  [sym_string] = "string",
  [sym__escape_interpolation] = "_escape_interpolation",
  [sym_boolean] = "boolean",
  [aux_sym_snakemake_repeat1] = "snakemake_repeat1",
  [aux_sym_input_repeat1] = "input_repeat1",
  [aux_sym__shellparams_repeat1] = "_shellparams_repeat1",
  [aux_sym_string_repeat1] = "string_repeat1",
  [anon_alias_sym_content] = "content",
};

static const TSSymbol ts_symbol_map[] = {
  [ts_builtin_sym_end] = ts_builtin_sym_end,
  [anon_sym_rule] = anon_sym_rule,
  [anon_sym_COLON] = anon_sym_COLON,
  [anon_sym_include_COLON] = anon_sym_include_COLON,
  [anon_sym_workdir_COLON] = anon_sym_workdir_COLON,
  [anon_sym_module] = anon_sym_module,
  [anon_sym_configfile] = anon_sym_configfile,
  [anon_sym_use] = anon_sym_use,
  [anon_sym_STAR] = anon_sym_STAR,
  [anon_sym_from] = anon_sym_from,
  [anon_sym_as] = anon_sym_as,
  [anon_sym_with] = anon_sym_with,
  [anon_sym_input] = anon_sym_input,
  [anon_sym_COMMA] = anon_sym_COMMA,
  [anon_sym_output] = anon_sym_output,
  [anon_sym_params] = anon_sym_params,
  [anon_sym_log] = anon_sym_log,
  [anon_sym_cache] = anon_sym_cache,
  [anon_sym_message] = anon_sym_message,
  [anon_sym_threads] = anon_sym_threads,
  [anon_sym_resources] = anon_sym_resources,
  [anon_sym_conda] = anon_sym_conda,
  [anon_sym_container] = anon_sym_container,
  [anon_sym_shell] = anon_sym_shell,
  [anon_sym_script] = anon_sym_script,
  [anon_sym_notebook] = anon_sym_notebook,
  [anon_sym_snakefile] = anon_sym_snakefile,
  [anon_sym_meta_wrapper] = anon_sym_meta_wrapper,
  [anon_sym_config] = anon_sym_config,
  [anon_sym_skip_validation] = anon_sym_skip_validation,
  [sym_comment] = sym_comment,
  [sym_identifier] = sym_identifier,
  [anon_sym_LBRACE_LBRACE] = anon_sym_LBRACE_LBRACE,
  [anon_sym_RBRACE_RBRACE] = anon_sym_RBRACE_RBRACE,
  [sym_escape_sequence] = sym_escape_sequence,
  [sym__not_escape_sequence] = sym__not_escape_sequence,
  [anon_sym_True] = anon_sym_True,
  [anon_sym_False] = anon_sym_False,
  [sym_integer] = sym_integer,
  [sym__newline] = sym__newline,
  [sym__indent] = sym__indent,
  [sym__dedent] = sym__dedent,
  [sym__string_start] = sym__string_start,
  [sym__string_content] = sym__string_content,
  [sym__string_end] = sym__string_start,
  [sym_snakemake] = sym_snakemake,
  [sym_rule] = sym_rule,
  [sym__include] = sym__include,
  [sym__workdir] = sym__workdir,
  [sym__configfile] = sym__configfile,
  [sym__ni] = sym__ni,
  [aux_sym__norunparams] = aux_sym__norunparams,
  [sym__ruleparams] = sym__ruleparams,
  [sym_input] = sym_input,
  [sym_output] = sym_output,
  [sym__params] = sym__params,
  [sym_log] = sym_log,
  [sym_cache] = sym_cache,
  [sym_message] = sym_message,
  [sym_threads] = sym_threads,
  [sym__resources] = sym__resources,
  [sym_conda] = sym_conda,
  [sym_container] = sym_container,
  [sym_shell] = sym_shell,
  [sym__shellparams] = sym__shellparams,
  [sym_script] = sym_script,
  [sym_notebook] = sym_notebook,
  [sym__parameter_list] = sym__parameter_list,
  [sym_string] = sym_string,
  [sym__escape_interpolation] = sym__escape_interpolation,
  [sym_boolean] = sym_boolean,
  [aux_sym_snakemake_repeat1] = aux_sym_snakemake_repeat1,
  [aux_sym_input_repeat1] = aux_sym_input_repeat1,
  [aux_sym__shellparams_repeat1] = aux_sym__shellparams_repeat1,
  [aux_sym_string_repeat1] = aux_sym_string_repeat1,
  [anon_alias_sym_content] = anon_alias_sym_content,
};

static const TSSymbolMetadata ts_symbol_metadata[] = {
  [ts_builtin_sym_end] = {
    .visible = false,
    .named = true,
  },
  [anon_sym_rule] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_COLON] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_include_COLON] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_workdir_COLON] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_module] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_configfile] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_use] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_STAR] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_from] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_as] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_with] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_input] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_COMMA] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_output] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_params] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_log] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_cache] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_message] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_threads] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_resources] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_conda] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_container] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_shell] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_script] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_notebook] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_snakefile] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_meta_wrapper] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_config] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_skip_validation] = {
    .visible = true,
    .named = false,
  },
  [sym_comment] = {
    .visible = true,
    .named = true,
  },
  [sym_identifier] = {
    .visible = true,
    .named = true,
  },
  [anon_sym_LBRACE_LBRACE] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_RBRACE_RBRACE] = {
    .visible = true,
    .named = false,
  },
  [sym_escape_sequence] = {
    .visible = true,
    .named = true,
  },
  [sym__not_escape_sequence] = {
    .visible = false,
    .named = true,
  },
  [anon_sym_True] = {
    .visible = true,
    .named = false,
  },
  [anon_sym_False] = {
    .visible = true,
    .named = false,
  },
  [sym_integer] = {
    .visible = true,
    .named = true,
  },
  [sym__newline] = {
    .visible = false,
    .named = true,
  },
  [sym__indent] = {
    .visible = false,
    .named = true,
  },
  [sym__dedent] = {
    .visible = false,
    .named = true,
  },
  [sym__string_start] = {
    .visible = true,
    .named = false,
  },
  [sym__string_content] = {
    .visible = false,
    .named = true,
  },
  [sym__string_end] = {
    .visible = true,
    .named = false,
  },
  [sym_snakemake] = {
    .visible = true,
    .named = true,
  },
  [sym_rule] = {
    .visible = true,
    .named = true,
  },
  [sym__include] = {
    .visible = false,
    .named = true,
  },
  [sym__workdir] = {
    .visible = false,
    .named = true,
  },
  [sym__configfile] = {
    .visible = false,
    .named = true,
  },
  [sym__ni] = {
    .visible = false,
    .named = true,
  },
  [aux_sym__norunparams] = {
    .visible = false,
    .named = false,
  },
  [sym__ruleparams] = {
    .visible = false,
    .named = true,
  },
  [sym_input] = {
    .visible = true,
    .named = true,
  },
  [sym_output] = {
    .visible = true,
    .named = true,
  },
  [sym__params] = {
    .visible = false,
    .named = true,
  },
  [sym_log] = {
    .visible = true,
    .named = true,
  },
  [sym_cache] = {
    .visible = true,
    .named = true,
  },
  [sym_message] = {
    .visible = true,
    .named = true,
  },
  [sym_threads] = {
    .visible = true,
    .named = true,
  },
  [sym__resources] = {
    .visible = false,
    .named = true,
  },
  [sym_conda] = {
    .visible = true,
    .named = true,
  },
  [sym_container] = {
    .visible = true,
    .named = true,
  },
  [sym_shell] = {
    .visible = true,
    .named = true,
  },
  [sym__shellparams] = {
    .visible = false,
    .named = true,
  },
  [sym_script] = {
    .visible = true,
    .named = true,
  },
  [sym_notebook] = {
    .visible = true,
    .named = true,
  },
  [sym__parameter_list] = {
    .visible = false,
    .named = true,
  },
  [sym_string] = {
    .visible = true,
    .named = true,
  },
  [sym__escape_interpolation] = {
    .visible = false,
    .named = true,
  },
  [sym_boolean] = {
    .visible = true,
    .named = true,
  },
  [aux_sym_snakemake_repeat1] = {
    .visible = false,
    .named = false,
  },
  [aux_sym_input_repeat1] = {
    .visible = false,
    .named = false,
  },
  [aux_sym__shellparams_repeat1] = {
    .visible = false,
    .named = false,
  },
  [aux_sym_string_repeat1] = {
    .visible = false,
    .named = false,
  },
  [anon_alias_sym_content] = {
    .visible = true,
    .named = false,
  },
};

enum {
  field_conda = 1,
  field_input = 2,
  field_name = 3,
  field_output = 4,
  field_params = 5,
  field_shell = 6,
};

static const char * const ts_field_names[] = {
  [0] = NULL,
  [field_conda] = "conda",
  [field_input] = "input",
  [field_name] = "name",
  [field_output] = "output",
  [field_params] = "params",
  [field_shell] = "shell",
};

static const TSFieldMapSlice ts_field_map_slices[PRODUCTION_ID_COUNT] = {
  [1] = {.index = 0, .length = 2},
  [3] = {.index = 2, .length = 1},
  [4] = {.index = 3, .length = 1},
  [5] = {.index = 4, .length = 2},
  [6] = {.index = 6, .length = 1},
  [7] = {.index = 7, .length = 1},
  [8] = {.index = 8, .length = 1},
  [9] = {.index = 9, .length = 1},
  [10] = {.index = 10, .length = 1},
  [11] = {.index = 11, .length = 2},
  [12] = {.index = 13, .length = 2},
  [13] = {.index = 15, .length = 2},
};

static const TSFieldMapEntry ts_field_map_entries[] = {
  [0] =
    {field_name, 1},
    {field_params, 3, .inherited = true},
  [2] =
    {field_params, 1, .inherited = true},
  [3] =
    {field_params, 0, .inherited = true},
  [4] =
    {field_params, 0, .inherited = true},
    {field_params, 1, .inherited = true},
  [6] =
    {field_input, 2},
  [7] =
    {field_output, 2},
  [8] =
    {field_params, 2},
  [9] =
    {field_conda, 2},
  [10] =
    {field_shell, 2},
  [11] =
    {field_input, 2},
    {field_input, 3},
  [13] =
    {field_output, 2},
    {field_output, 3},
  [15] =
    {field_conda, 2},
    {field_conda, 3},
};

static const TSSymbol ts_alias_sequences[PRODUCTION_ID_COUNT][MAX_ALIAS_SEQUENCE_LENGTH] = {
  [0] = {0},
  [2] = {
    [1] = anon_alias_sym_content,
  },
};

static const uint16_t ts_non_terminal_alias_map[] = {
  aux_sym_string_repeat1, 2,
    aux_sym_string_repeat1,
    anon_alias_sym_content,
  0,
};

static inline bool sym_identifier_character_set_1(int32_t c) {
  return (c < 43514
    ? (c < 4193
      ? (c < 2707
        ? (c < 1994
          ? (c < 910
            ? (c < 736
              ? (c < 186
                ? (c < 'a'
                  ? (c < '_'
                    ? (c >= 'A' && c <= 'Z')
                    : c <= '_')
                  : (c <= 'z' || (c < 181
                    ? c == 170
                    : c <= 181)))
                : (c <= 186 || (c < 248
                  ? (c < 216
                    ? (c >= 192 && c <= 214)
                    : c <= 246)
                  : (c <= 705 || (c >= 710 && c <= 721)))))
              : (c <= 740 || (c < 891
                ? (c < 880
                  ? (c < 750
                    ? c == 748
                    : c <= 750)
                  : (c <= 884 || (c >= 886 && c <= 887)))
                : (c <= 893 || (c < 904
                  ? (c < 902
                    ? c == 895
                    : c <= 902)
                  : (c <= 906 || c == 908))))))
            : (c <= 929 || (c < 1649
              ? (c < 1376
                ? (c < 1162
                  ? (c < 1015
                    ? (c >= 931 && c <= 1013)
                    : c <= 1153)
                  : (c <= 1327 || (c < 1369
                    ? (c >= 1329 && c <= 1366)
                    : c <= 1369)))
                : (c <= 1416 || (c < 1568
                  ? (c < 1519
                    ? (c >= 1488 && c <= 1514)
                    : c <= 1522)
                  : (c <= 1610 || (c >= 1646 && c <= 1647)))))
              : (c <= 1747 || (c < 1791
                ? (c < 1774
                  ? (c < 1765
                    ? c == 1749
                    : c <= 1766)
                  : (c <= 1775 || (c >= 1786 && c <= 1788)))
                : (c <= 1791 || (c < 1869
                  ? (c < 1810
                    ? c == 1808
                    : c <= 1839)
                  : (c <= 1957 || c == 1969))))))))
          : (c <= 2026 || (c < 2482
            ? (c < 2208
              ? (c < 2088
                ? (c < 2048
                  ? (c < 2042
                    ? (c >= 2036 && c <= 2037)
                    : c <= 2042)
                  : (c <= 2069 || (c < 2084
                    ? c == 2074
                    : c <= 2084)))
                : (c <= 2088 || (c < 2160
                  ? (c < 2144
                    ? (c >= 2112 && c <= 2136)
                    : c <= 2154)
                  : (c <= 2183 || (c >= 2185 && c <= 2190)))))
              : (c <= 2249 || (c < 2417
                ? (c < 2384
                  ? (c < 2365
                    ? (c >= 2308 && c <= 2361)
                    : c <= 2365)
                  : (c <= 2384 || (c >= 2392 && c <= 2401)))
                : (c <= 2432 || (c < 2451
                  ? (c < 2447
                    ? (c >= 2437 && c <= 2444)
                    : c <= 2448)
                  : (c <= 2472 || (c >= 2474 && c <= 2480)))))))
            : (c <= 2482 || (c < 2579
              ? (c < 2527
                ? (c < 2510
                  ? (c < 2493
                    ? (c >= 2486 && c <= 2489)
                    : c <= 2493)
                  : (c <= 2510 || (c >= 2524 && c <= 2525)))
                : (c <= 2529 || (c < 2565
                  ? (c < 2556
                    ? (c >= 2544 && c <= 2545)
                    : c <= 2556)
                  : (c <= 2570 || (c >= 2575 && c <= 2576)))))
              : (c <= 2600 || (c < 2649
                ? (c < 2613
                  ? (c < 2610
                    ? (c >= 2602 && c <= 2608)
                    : c <= 2611)
                  : (c <= 2614 || (c >= 2616 && c <= 2617)))
                : (c <= 2652 || (c < 2693
                  ? (c < 2674
                    ? c == 2654
                    : c <= 2676)
                  : (c <= 2701 || (c >= 2703 && c <= 2705)))))))))))
        : (c <= 2728 || (c < 3242
          ? (c < 2962
            ? (c < 2858
              ? (c < 2784
                ? (c < 2741
                  ? (c < 2738
                    ? (c >= 2730 && c <= 2736)
                    : c <= 2739)
                  : (c <= 2745 || (c < 2768
                    ? c == 2749
                    : c <= 2768)))
                : (c <= 2785 || (c < 2831
                  ? (c < 2821
                    ? c == 2809
                    : c <= 2828)
                  : (c <= 2832 || (c >= 2835 && c <= 2856)))))
              : (c <= 2864 || (c < 2911
                ? (c < 2877
                  ? (c < 2869
                    ? (c >= 2866 && c <= 2867)
                    : c <= 2873)
                  : (c <= 2877 || (c >= 2908 && c <= 2909)))
                : (c <= 2913 || (c < 2949
                  ? (c < 2947
                    ? c == 2929
                    : c <= 2947)
                  : (c <= 2954 || (c >= 2958 && c <= 2960)))))))
            : (c <= 2965 || (c < 3090
              ? (c < 2984
                ? (c < 2974
                  ? (c < 2972
                    ? (c >= 2969 && c <= 2970)
                    : c <= 2972)
                  : (c <= 2975 || (c >= 2979 && c <= 2980)))
                : (c <= 2986 || (c < 3077
                  ? (c < 3024
                    ? (c >= 2990 && c <= 3001)
                    : c <= 3024)
                  : (c <= 3084 || (c >= 3086 && c <= 3088)))))
              : (c <= 3112 || (c < 3168
                ? (c < 3160
                  ? (c < 3133
                    ? (c >= 3114 && c <= 3129)
                    : c <= 3133)
                  : (c <= 3162 || c == 3165))
                : (c <= 3169 || (c < 3214
                  ? (c < 3205
                    ? c == 3200
                    : c <= 3212)
                  : (c <= 3216 || (c >= 3218 && c <= 3240)))))))))
          : (c <= 3251 || (c < 3648
            ? (c < 3412
              ? (c < 3332
                ? (c < 3293
                  ? (c < 3261
                    ? (c >= 3253 && c <= 3257)
                    : c <= 3261)
                  : (c <= 3294 || (c < 3313
                    ? (c >= 3296 && c <= 3297)
                    : c <= 3314)))
                : (c <= 3340 || (c < 3389
                  ? (c < 3346
                    ? (c >= 3342 && c <= 3344)
                    : c <= 3386)
                  : (c <= 3389 || c == 3406))))
              : (c <= 3414 || (c < 3507
                ? (c < 3461
                  ? (c < 3450
                    ? (c >= 3423 && c <= 3425)
                    : c <= 3455)
                  : (c <= 3478 || (c >= 3482 && c <= 3505)))
                : (c <= 3515 || (c < 3585
                  ? (c < 3520
                    ? c == 3517
                    : c <= 3526)
                  : (c <= 3632 || c == 3634))))))
            : (c <= 3654 || (c < 3782
              ? (c < 3749
                ? (c < 3718
                  ? (c < 3716
                    ? (c >= 3713 && c <= 3714)
                    : c <= 3716)
                  : (c <= 3722 || (c >= 3724 && c <= 3747)))
                : (c <= 3749 || (c < 3773
                  ? (c < 3762
                    ? (c >= 3751 && c <= 3760)
                    : c <= 3762)
                  : (c <= 3773 || (c >= 3776 && c <= 3780)))))
              : (c <= 3782 || (c < 3976
                ? (c < 3904
                  ? (c < 3840
                    ? (c >= 3804 && c <= 3807)
                    : c <= 3840)
                  : (c <= 3911 || (c >= 3913 && c <= 3948)))
                : (c <= 3980 || (c < 4176
                  ? (c < 4159
                    ? (c >= 4096 && c <= 4138)
                    : c <= 4159)
                  : (c <= 4181 || (c >= 4186 && c <= 4189)))))))))))))
      : (c <= 4193 || (c < 8134
        ? (c < 6176
          ? (c < 4808
            ? (c < 4688
              ? (c < 4295
                ? (c < 4213
                  ? (c < 4206
                    ? (c >= 4197 && c <= 4198)
                    : c <= 4208)
                  : (c <= 4225 || (c < 4256
                    ? c == 4238
                    : c <= 4293)))
                : (c <= 4295 || (c < 4348
                  ? (c < 4304
                    ? c == 4301
                    : c <= 4346)
                  : (c <= 4680 || (c >= 4682 && c <= 4685)))))
              : (c <= 4694 || (c < 4752
                ? (c < 4704
                  ? (c < 4698
                    ? c == 4696
                    : c <= 4701)
                  : (c <= 4744 || (c >= 4746 && c <= 4749)))
                : (c <= 4784 || (c < 4800
                  ? (c < 4792
                    ? (c >= 4786 && c <= 4789)
                    : c <= 4798)
                  : (c <= 4800 || (c >= 4802 && c <= 4805)))))))
            : (c <= 4822 || (c < 5792
              ? (c < 5024
                ? (c < 4888
                  ? (c < 4882
                    ? (c >= 4824 && c <= 4880)
                    : c <= 4885)
                  : (c <= 4954 || (c >= 4992 && c <= 5007)))
                : (c <= 5109 || (c < 5743
                  ? (c < 5121
                    ? (c >= 5112 && c <= 5117)
                    : c <= 5740)
                  : (c <= 5759 || (c >= 5761 && c <= 5786)))))
              : (c <= 5866 || (c < 5984
                ? (c < 5919
                  ? (c < 5888
                    ? (c >= 5870 && c <= 5880)
                    : c <= 5905)
                  : (c <= 5937 || (c >= 5952 && c <= 5969)))
                : (c <= 5996 || (c < 6103
                  ? (c < 6016
                    ? (c >= 5998 && c <= 6000)
                    : c <= 6067)
                  : (c <= 6103 || c == 6108))))))))
          : (c <= 6264 || (c < 7312
            ? (c < 6823
              ? (c < 6512
                ? (c < 6320
                  ? (c < 6314
                    ? (c >= 6272 && c <= 6312)
                    : c <= 6314)
                  : (c <= 6389 || (c < 6480
                    ? (c >= 6400 && c <= 6430)
                    : c <= 6509)))
                : (c <= 6516 || (c < 6656
                  ? (c < 6576
                    ? (c >= 6528 && c <= 6571)
                    : c <= 6601)
                  : (c <= 6678 || (c >= 6688 && c <= 6740)))))
              : (c <= 6823 || (c < 7098
                ? (c < 7043
                  ? (c < 6981
                    ? (c >= 6917 && c <= 6963)
                    : c <= 6988)
                  : (c <= 7072 || (c >= 7086 && c <= 7087)))
                : (c <= 7141 || (c < 7258
                  ? (c < 7245
                    ? (c >= 7168 && c <= 7203)
                    : c <= 7247)
                  : (c <= 7293 || (c >= 7296 && c <= 7304)))))))
            : (c <= 7354 || (c < 8008
              ? (c < 7418
                ? (c < 7406
                  ? (c < 7401
                    ? (c >= 7357 && c <= 7359)
                    : c <= 7404)
                  : (c <= 7411 || (c >= 7413 && c <= 7414)))
                : (c <= 7418 || (c < 7960
                  ? (c < 7680
                    ? (c >= 7424 && c <= 7615)
                    : c <= 7957)
                  : (c <= 7965 || (c >= 7968 && c <= 8005)))))
              : (c <= 8013 || (c < 8031
                ? (c < 8027
                  ? (c < 8025
                    ? (c >= 8016 && c <= 8023)
                    : c <= 8025)
                  : (c <= 8027 || c == 8029))
                : (c <= 8061 || (c < 8126
                  ? (c < 8118
                    ? (c >= 8064 && c <= 8116)
                    : c <= 8124)
                  : (c <= 8126 || (c >= 8130 && c <= 8132)))))))))))
        : (c <= 8140 || (c < 12337
          ? (c < 8544
            ? (c < 8458
              ? (c < 8305
                ? (c < 8160
                  ? (c < 8150
                    ? (c >= 8144 && c <= 8147)
                    : c <= 8155)
                  : (c <= 8172 || (c < 8182
                    ? (c >= 8178 && c <= 8180)
                    : c <= 8188)))
                : (c <= 8305 || (c < 8450
                  ? (c < 8336
                    ? c == 8319
                    : c <= 8348)
                  : (c <= 8450 || c == 8455))))
              : (c <= 8467 || (c < 8488
                ? (c < 8484
                  ? (c < 8472
                    ? c == 8469
                    : c <= 8477)
                  : (c <= 8484 || c == 8486))
                : (c <= 8488 || (c < 8517
                  ? (c < 8508
                    ? (c >= 8490 && c <= 8505)
                    : c <= 8511)
                  : (c <= 8521 || c == 8526))))))
            : (c <= 8584 || (c < 11680
              ? (c < 11559
                ? (c < 11506
                  ? (c < 11499
                    ? (c >= 11264 && c <= 11492)
                    : c <= 11502)
                  : (c <= 11507 || (c >= 11520 && c <= 11557)))
                : (c <= 11559 || (c < 11631
                  ? (c < 11568
                    ? c == 11565
                    : c <= 11623)
                  : (c <= 11631 || (c >= 11648 && c <= 11670)))))
              : (c <= 11686 || (c < 11720
                ? (c < 11704
                  ? (c < 11696
                    ? (c >= 11688 && c <= 11694)
                    : c <= 11702)
                  : (c <= 11710 || (c >= 11712 && c <= 11718)))
                : (c <= 11726 || (c < 12293
                  ? (c < 11736
                    ? (c >= 11728 && c <= 11734)
                    : c <= 11742)
                  : (c <= 12295 || (c >= 12321 && c <= 12329)))))))))
          : (c <= 12341 || (c < 42891
            ? (c < 19968
              ? (c < 12549
                ? (c < 12445
                  ? (c < 12353
                    ? (c >= 12344 && c <= 12348)
                    : c <= 12438)
                  : (c <= 12447 || (c < 12540
                    ? (c >= 12449 && c <= 12538)
                    : c <= 12543)))
                : (c <= 12591 || (c < 12784
                  ? (c < 12704
                    ? (c >= 12593 && c <= 12686)
                    : c <= 12735)
                  : (c <= 12799 || (c >= 13312 && c <= 19903)))))
              : (c <= 42124 || (c < 42560
                ? (c < 42512
                  ? (c < 42240
                    ? (c >= 42192 && c <= 42237)
                    : c <= 42508)
                  : (c <= 42527 || (c >= 42538 && c <= 42539)))
                : (c <= 42606 || (c < 42775
                  ? (c < 42656
                    ? (c >= 42623 && c <= 42653)
                    : c <= 42735)
                  : (c <= 42783 || (c >= 42786 && c <= 42888)))))))
            : (c <= 42954 || (c < 43250
              ? (c < 43011
                ? (c < 42965
                  ? (c < 42963
                    ? (c >= 42960 && c <= 42961)
                    : c <= 42963)
                  : (c <= 42969 || (c >= 42994 && c <= 43009)))
                : (c <= 43013 || (c < 43072
                  ? (c < 43020
                    ? (c >= 43015 && c <= 43018)
                    : c <= 43042)
                  : (c <= 43123 || (c >= 43138 && c <= 43187)))))
              : (c <= 43255 || (c < 43360
                ? (c < 43274
                  ? (c < 43261
                    ? c == 43259
                    : c <= 43262)
                  : (c <= 43301 || (c >= 43312 && c <= 43334)))
                : (c <= 43388 || (c < 43488
                  ? (c < 43471
                    ? (c >= 43396 && c <= 43442)
                    : c <= 43471)
                  : (c <= 43492 || (c >= 43494 && c <= 43503)))))))))))))))
    : (c <= 43518 || (c < 70727
      ? (c < 66956
        ? (c < 64914
          ? (c < 43868
            ? (c < 43714
              ? (c < 43646
                ? (c < 43588
                  ? (c < 43584
                    ? (c >= 43520 && c <= 43560)
                    : c <= 43586)
                  : (c <= 43595 || (c < 43642
                    ? (c >= 43616 && c <= 43638)
                    : c <= 43642)))
                : (c <= 43695 || (c < 43705
                  ? (c < 43701
                    ? c == 43697
                    : c <= 43702)
                  : (c <= 43709 || c == 43712))))
              : (c <= 43714 || (c < 43785
                ? (c < 43762
                  ? (c < 43744
                    ? (c >= 43739 && c <= 43741)
                    : c <= 43754)
                  : (c <= 43764 || (c >= 43777 && c <= 43782)))
                : (c <= 43790 || (c < 43816
                  ? (c < 43808
                    ? (c >= 43793 && c <= 43798)
                    : c <= 43814)
                  : (c <= 43822 || (c >= 43824 && c <= 43866)))))))
            : (c <= 43881 || (c < 64287
              ? (c < 63744
                ? (c < 55216
                  ? (c < 44032
                    ? (c >= 43888 && c <= 44002)
                    : c <= 55203)
                  : (c <= 55238 || (c >= 55243 && c <= 55291)))
                : (c <= 64109 || (c < 64275
                  ? (c < 64256
                    ? (c >= 64112 && c <= 64217)
                    : c <= 64262)
                  : (c <= 64279 || c == 64285))))
              : (c <= 64296 || (c < 64323
                ? (c < 64318
                  ? (c < 64312
                    ? (c >= 64298 && c <= 64310)
                    : c <= 64316)
                  : (c <= 64318 || (c >= 64320 && c <= 64321)))
                : (c <= 64324 || (c < 64612
                  ? (c < 64467
                    ? (c >= 64326 && c <= 64433)
                    : c <= 64605)
                  : (c <= 64829 || (c >= 64848 && c <= 64911)))))))))
          : (c <= 64967 || (c < 65599
            ? (c < 65382
              ? (c < 65147
                ? (c < 65139
                  ? (c < 65137
                    ? (c >= 65008 && c <= 65017)
                    : c <= 65137)
                  : (c <= 65139 || (c < 65145
                    ? c == 65143
                    : c <= 65145)))
                : (c <= 65147 || (c < 65313
                  ? (c < 65151
                    ? c == 65149
                    : c <= 65276)
                  : (c <= 65338 || (c >= 65345 && c <= 65370)))))
              : (c <= 65437 || (c < 65498
                ? (c < 65482
                  ? (c < 65474
                    ? (c >= 65440 && c <= 65470)
                    : c <= 65479)
                  : (c <= 65487 || (c >= 65490 && c <= 65495)))
                : (c <= 65500 || (c < 65576
                  ? (c < 65549
                    ? (c >= 65536 && c <= 65547)
                    : c <= 65574)
                  : (c <= 65594 || (c >= 65596 && c <= 65597)))))))
            : (c <= 65613 || (c < 66464
              ? (c < 66208
                ? (c < 65856
                  ? (c < 65664
                    ? (c >= 65616 && c <= 65629)
                    : c <= 65786)
                  : (c <= 65908 || (c >= 66176 && c <= 66204)))
                : (c <= 66256 || (c < 66384
                  ? (c < 66349
                    ? (c >= 66304 && c <= 66335)
                    : c <= 66378)
                  : (c <= 66421 || (c >= 66432 && c <= 66461)))))
              : (c <= 66499 || (c < 66776
                ? (c < 66560
                  ? (c < 66513
                    ? (c >= 66504 && c <= 66511)
                    : c <= 66517)
                  : (c <= 66717 || (c >= 66736 && c <= 66771)))
                : (c <= 66811 || (c < 66928
                  ? (c < 66864
                    ? (c >= 66816 && c <= 66855)
                    : c <= 66915)
                  : (c <= 66938 || (c >= 66940 && c <= 66954)))))))))))
        : (c <= 66962 || (c < 68864
          ? (c < 67828
            ? (c < 67506
              ? (c < 67072
                ? (c < 66979
                  ? (c < 66967
                    ? (c >= 66964 && c <= 66965)
                    : c <= 66977)
                  : (c <= 66993 || (c < 67003
                    ? (c >= 66995 && c <= 67001)
                    : c <= 67004)))
                : (c <= 67382 || (c < 67456
                  ? (c < 67424
                    ? (c >= 67392 && c <= 67413)
                    : c <= 67431)
                  : (c <= 67461 || (c >= 67463 && c <= 67504)))))
              : (c <= 67514 || (c < 67644
                ? (c < 67594
                  ? (c < 67592
                    ? (c >= 67584 && c <= 67589)
                    : c <= 67592)
                  : (c <= 67637 || (c >= 67639 && c <= 67640)))
                : (c <= 67644 || (c < 67712
                  ? (c < 67680
                    ? (c >= 67647 && c <= 67669)
                    : c <= 67702)
                  : (c <= 67742 || (c >= 67808 && c <= 67826)))))))
            : (c <= 67829 || (c < 68224
              ? (c < 68096
                ? (c < 67968
                  ? (c < 67872
                    ? (c >= 67840 && c <= 67861)
                    : c <= 67897)
                  : (c <= 68023 || (c >= 68030 && c <= 68031)))
                : (c <= 68096 || (c < 68121
                  ? (c < 68117
                    ? (c >= 68112 && c <= 68115)
                    : c <= 68119)
                  : (c <= 68149 || (c >= 68192 && c <= 68220)))))
              : (c <= 68252 || (c < 68448
                ? (c < 68352
                  ? (c < 68297
                    ? (c >= 68288 && c <= 68295)
                    : c <= 68324)
                  : (c <= 68405 || (c >= 68416 && c <= 68437)))
                : (c <= 68466 || (c < 68736
                  ? (c < 68608
                    ? (c >= 68480 && c <= 68497)
                    : c <= 68680)
                  : (c <= 68786 || (c >= 68800 && c <= 68850)))))))))
          : (c <= 68899 || (c < 70106
            ? (c < 69749
              ? (c < 69488
                ? (c < 69376
                  ? (c < 69296
                    ? (c >= 69248 && c <= 69289)
                    : c <= 69297)
                  : (c <= 69404 || (c < 69424
                    ? c == 69415
                    : c <= 69445)))
                : (c <= 69505 || (c < 69635
                  ? (c < 69600
                    ? (c >= 69552 && c <= 69572)
                    : c <= 69622)
                  : (c <= 69687 || (c >= 69745 && c <= 69746)))))
              : (c <= 69749 || (c < 69959
                ? (c < 69891
                  ? (c < 69840
                    ? (c >= 69763 && c <= 69807)
                    : c <= 69864)
                  : (c <= 69926 || c == 69956))
                : (c <= 69959 || (c < 70019
                  ? (c < 70006
                    ? (c >= 69968 && c <= 70002)
                    : c <= 70006)
                  : (c <= 70066 || (c >= 70081 && c <= 70084)))))))
            : (c <= 70106 || (c < 70405
              ? (c < 70280
                ? (c < 70163
                  ? (c < 70144
                    ? c == 70108
                    : c <= 70161)
                  : (c <= 70187 || (c >= 70272 && c <= 70278)))
                : (c <= 70280 || (c < 70303
                  ? (c < 70287
                    ? (c >= 70282 && c <= 70285)
                    : c <= 70301)
                  : (c <= 70312 || (c >= 70320 && c <= 70366)))))
              : (c <= 70412 || (c < 70453
                ? (c < 70442
                  ? (c < 70419
                    ? (c >= 70415 && c <= 70416)
                    : c <= 70440)
                  : (c <= 70448 || (c >= 70450 && c <= 70451)))
                : (c <= 70457 || (c < 70493
                  ? (c < 70480
                    ? c == 70461
                    : c <= 70480)
                  : (c <= 70497 || (c >= 70656 && c <= 70708)))))))))))))
      : (c <= 70730 || (c < 119894
        ? (c < 73056
          ? (c < 72001
            ? (c < 71424
              ? (c < 71128
                ? (c < 70852
                  ? (c < 70784
                    ? (c >= 70751 && c <= 70753)
                    : c <= 70831)
                  : (c <= 70853 || (c < 71040
                    ? c == 70855
                    : c <= 71086)))
                : (c <= 71131 || (c < 71296
                  ? (c < 71236
                    ? (c >= 71168 && c <= 71215)
                    : c <= 71236)
                  : (c <= 71338 || c == 71352))))
              : (c <= 71450 || (c < 71945
                ? (c < 71840
                  ? (c < 71680
                    ? (c >= 71488 && c <= 71494)
                    : c <= 71723)
                  : (c <= 71903 || (c >= 71935 && c <= 71942)))
                : (c <= 71945 || (c < 71960
                  ? (c < 71957
                    ? (c >= 71948 && c <= 71955)
                    : c <= 71958)
                  : (c <= 71983 || c == 71999))))))
            : (c <= 72001 || (c < 72349
              ? (c < 72192
                ? (c < 72161
                  ? (c < 72106
                    ? (c >= 72096 && c <= 72103)
                    : c <= 72144)
                  : (c <= 72161 || c == 72163))
                : (c <= 72192 || (c < 72272
                  ? (c < 72250
                    ? (c >= 72203 && c <= 72242)
                    : c <= 72250)
                  : (c <= 72272 || (c >= 72284 && c <= 72329)))))
              : (c <= 72349 || (c < 72818
                ? (c < 72714
                  ? (c < 72704
                    ? (c >= 72368 && c <= 72440)
                    : c <= 72712)
                  : (c <= 72750 || c == 72768))
                : (c <= 72847 || (c < 72971
                  ? (c < 72968
                    ? (c >= 72960 && c <= 72966)
                    : c <= 72969)
                  : (c <= 73008 || c == 73030))))))))
          : (c <= 73061 || (c < 93952
            ? (c < 82944
              ? (c < 73728
                ? (c < 73112
                  ? (c < 73066
                    ? (c >= 73063 && c <= 73064)
                    : c <= 73097)
                  : (c <= 73112 || (c < 73648
                    ? (c >= 73440 && c <= 73458)
                    : c <= 73648)))
                : (c <= 74649 || (c < 77712
                  ? (c < 74880
                    ? (c >= 74752 && c <= 74862)
                    : c <= 75075)
                  : (c <= 77808 || (c >= 77824 && c <= 78894)))))
              : (c <= 83526 || (c < 92928
                ? (c < 92784
                  ? (c < 92736
                    ? (c >= 92160 && c <= 92728)
                    : c <= 92766)
                  : (c <= 92862 || (c >= 92880 && c <= 92909)))
                : (c <= 92975 || (c < 93053
                  ? (c < 93027
                    ? (c >= 92992 && c <= 92995)
                    : c <= 93047)
                  : (c <= 93071 || (c >= 93760 && c <= 93823)))))))
            : (c <= 94026 || (c < 110589
              ? (c < 94208
                ? (c < 94176
                  ? (c < 94099
                    ? c == 94032
                    : c <= 94111)
                  : (c <= 94177 || c == 94179))
                : (c <= 100343 || (c < 110576
                  ? (c < 101632
                    ? (c >= 100352 && c <= 101589)
                    : c <= 101640)
                  : (c <= 110579 || (c >= 110581 && c <= 110587)))))
              : (c <= 110590 || (c < 113664
                ? (c < 110948
                  ? (c < 110928
                    ? (c >= 110592 && c <= 110882)
                    : c <= 110930)
                  : (c <= 110951 || (c >= 110960 && c <= 111355)))
                : (c <= 113770 || (c < 113808
                  ? (c < 113792
                    ? (c >= 113776 && c <= 113788)
                    : c <= 113800)
                  : (c <= 113817 || (c >= 119808 && c <= 119892)))))))))))
        : (c <= 119964 || (c < 125259
          ? (c < 120572
            ? (c < 120086
              ? (c < 119995
                ? (c < 119973
                  ? (c < 119970
                    ? (c >= 119966 && c <= 119967)
                    : c <= 119970)
                  : (c <= 119974 || (c < 119982
                    ? (c >= 119977 && c <= 119980)
                    : c <= 119993)))
                : (c <= 119995 || (c < 120071
                  ? (c < 120005
                    ? (c >= 119997 && c <= 120003)
                    : c <= 120069)
                  : (c <= 120074 || (c >= 120077 && c <= 120084)))))
              : (c <= 120092 || (c < 120138
                ? (c < 120128
                  ? (c < 120123
                    ? (c >= 120094 && c <= 120121)
                    : c <= 120126)
                  : (c <= 120132 || c == 120134))
                : (c <= 120144 || (c < 120514
                  ? (c < 120488
                    ? (c >= 120146 && c <= 120485)
                    : c <= 120512)
                  : (c <= 120538 || (c >= 120540 && c <= 120570)))))))
            : (c <= 120596 || (c < 123191
              ? (c < 120714
                ? (c < 120656
                  ? (c < 120630
                    ? (c >= 120598 && c <= 120628)
                    : c <= 120654)
                  : (c <= 120686 || (c >= 120688 && c <= 120712)))
                : (c <= 120744 || (c < 122624
                  ? (c < 120772
                    ? (c >= 120746 && c <= 120770)
                    : c <= 120779)
                  : (c <= 122654 || (c >= 123136 && c <= 123180)))))
              : (c <= 123197 || (c < 124904
                ? (c < 123584
                  ? (c < 123536
                    ? c == 123214
                    : c <= 123565)
                  : (c <= 123627 || (c >= 124896 && c <= 124902)))
                : (c <= 124907 || (c < 124928
                  ? (c < 124912
                    ? (c >= 124909 && c <= 124910)
                    : c <= 124926)
                  : (c <= 125124 || (c >= 125184 && c <= 125251)))))))))
          : (c <= 125259 || (c < 126559
            ? (c < 126535
              ? (c < 126505
                ? (c < 126497
                  ? (c < 126469
                    ? (c >= 126464 && c <= 126467)
                    : c <= 126495)
                  : (c <= 126498 || (c < 126503
                    ? c == 126500
                    : c <= 126503)))
                : (c <= 126514 || (c < 126523
                  ? (c < 126521
                    ? (c >= 126516 && c <= 126519)
                    : c <= 126521)
                  : (c <= 126523 || c == 126530))))
              : (c <= 126535 || (c < 126548
                ? (c < 126541
                  ? (c < 126539
                    ? c == 126537
                    : c <= 126539)
                  : (c <= 126543 || (c >= 126545 && c <= 126546)))
                : (c <= 126548 || (c < 126555
                  ? (c < 126553
                    ? c == 126551
                    : c <= 126553)
                  : (c <= 126555 || c == 126557))))))
            : (c <= 126559 || (c < 126625
              ? (c < 126580
                ? (c < 126567
                  ? (c < 126564
                    ? (c >= 126561 && c <= 126562)
                    : c <= 126564)
                  : (c <= 126570 || (c >= 126572 && c <= 126578)))
                : (c <= 126583 || (c < 126592
                  ? (c < 126590
                    ? (c >= 126585 && c <= 126588)
                    : c <= 126590)
                  : (c <= 126601 || (c >= 126603 && c <= 126619)))))
              : (c <= 126627 || (c < 177984
                ? (c < 131072
                  ? (c < 126635
                    ? (c >= 126629 && c <= 126633)
                    : c <= 126651)
                  : (c <= 173791 || (c >= 173824 && c <= 177976)))
                : (c <= 178205 || (c < 194560
                  ? (c < 183984
                    ? (c >= 178208 && c <= 183969)
                    : c <= 191456)
                  : (c <= 195101 || (c >= 196608 && c <= 201546)))))))))))))))));
}

static inline bool sym_identifier_character_set_2(int32_t c) {
  return (c < 43616
    ? (c < 3782
      ? (c < 2748
        ? (c < 2045
          ? (c < 1015
            ? (c < 710
              ? (c < 181
                ? (c < '_'
                  ? (c < 'A'
                    ? (c >= '0' && c <= '9')
                    : c <= 'Z')
                  : (c <= '_' || (c < 170
                    ? (c >= 'a' && c <= 'z')
                    : c <= 170)))
                : (c <= 181 || (c < 192
                  ? (c < 186
                    ? c == 183
                    : c <= 186)
                  : (c <= 214 || (c < 248
                    ? (c >= 216 && c <= 246)
                    : c <= 705)))))
              : (c <= 721 || (c < 891
                ? (c < 750
                  ? (c < 748
                    ? (c >= 736 && c <= 740)
                    : c <= 748)
                  : (c <= 750 || (c < 886
                    ? (c >= 768 && c <= 884)
                    : c <= 887)))
                : (c <= 893 || (c < 908
                  ? (c < 902
                    ? c == 895
                    : c <= 906)
                  : (c <= 908 || (c < 931
                    ? (c >= 910 && c <= 929)
                    : c <= 1013)))))))
            : (c <= 1153 || (c < 1519
              ? (c < 1425
                ? (c < 1329
                  ? (c < 1162
                    ? (c >= 1155 && c <= 1159)
                    : c <= 1327)
                  : (c <= 1366 || (c < 1376
                    ? c == 1369
                    : c <= 1416)))
                : (c <= 1469 || (c < 1476
                  ? (c < 1473
                    ? c == 1471
                    : c <= 1474)
                  : (c <= 1477 || (c < 1488
                    ? c == 1479
                    : c <= 1514)))))
              : (c <= 1522 || (c < 1770
                ? (c < 1646
                  ? (c < 1568
                    ? (c >= 1552 && c <= 1562)
                    : c <= 1641)
                  : (c <= 1747 || (c < 1759
                    ? (c >= 1749 && c <= 1756)
                    : c <= 1768)))
                : (c <= 1788 || (c < 1869
                  ? (c < 1808
                    ? c == 1791
                    : c <= 1866)
                  : (c <= 1969 || (c < 2042
                    ? (c >= 1984 && c <= 2037)
                    : c <= 2042)))))))))
          : (c <= 2045 || (c < 2558
            ? (c < 2451
              ? (c < 2200
                ? (c < 2144
                  ? (c < 2112
                    ? (c >= 2048 && c <= 2093)
                    : c <= 2139)
                  : (c <= 2154 || (c < 2185
                    ? (c >= 2160 && c <= 2183)
                    : c <= 2190)))
                : (c <= 2273 || (c < 2417
                  ? (c < 2406
                    ? (c >= 2275 && c <= 2403)
                    : c <= 2415)
                  : (c <= 2435 || (c < 2447
                    ? (c >= 2437 && c <= 2444)
                    : c <= 2448)))))
              : (c <= 2472 || (c < 2507
                ? (c < 2486
                  ? (c < 2482
                    ? (c >= 2474 && c <= 2480)
                    : c <= 2482)
                  : (c <= 2489 || (c < 2503
                    ? (c >= 2492 && c <= 2500)
                    : c <= 2504)))
                : (c <= 2510 || (c < 2527
                  ? (c < 2524
                    ? c == 2519
                    : c <= 2525)
                  : (c <= 2531 || (c < 2556
                    ? (c >= 2534 && c <= 2545)
                    : c <= 2556)))))))
            : (c <= 2558 || (c < 2635
              ? (c < 2610
                ? (c < 2575
                  ? (c < 2565
                    ? (c >= 2561 && c <= 2563)
                    : c <= 2570)
                  : (c <= 2576 || (c < 2602
                    ? (c >= 2579 && c <= 2600)
                    : c <= 2608)))
                : (c <= 2611 || (c < 2620
                  ? (c < 2616
                    ? (c >= 2613 && c <= 2614)
                    : c <= 2617)
                  : (c <= 2620 || (c < 2631
                    ? (c >= 2622 && c <= 2626)
                    : c <= 2632)))))
              : (c <= 2637 || (c < 2693
                ? (c < 2654
                  ? (c < 2649
                    ? c == 2641
                    : c <= 2652)
                  : (c <= 2654 || (c < 2689
                    ? (c >= 2662 && c <= 2677)
                    : c <= 2691)))
                : (c <= 2701 || (c < 2730
                  ? (c < 2707
                    ? (c >= 2703 && c <= 2705)
                    : c <= 2728)
                  : (c <= 2736 || (c < 2741
                    ? (c >= 2738 && c <= 2739)
                    : c <= 2745)))))))))))
        : (c <= 2757 || (c < 3168
          ? (c < 2958
            ? (c < 2866
              ? (c < 2809
                ? (c < 2768
                  ? (c < 2763
                    ? (c >= 2759 && c <= 2761)
                    : c <= 2765)
                  : (c <= 2768 || (c < 2790
                    ? (c >= 2784 && c <= 2787)
                    : c <= 2799)))
                : (c <= 2815 || (c < 2831
                  ? (c < 2821
                    ? (c >= 2817 && c <= 2819)
                    : c <= 2828)
                  : (c <= 2832 || (c < 2858
                    ? (c >= 2835 && c <= 2856)
                    : c <= 2864)))))
              : (c <= 2867 || (c < 2908
                ? (c < 2887
                  ? (c < 2876
                    ? (c >= 2869 && c <= 2873)
                    : c <= 2884)
                  : (c <= 2888 || (c < 2901
                    ? (c >= 2891 && c <= 2893)
                    : c <= 2903)))
                : (c <= 2909 || (c < 2929
                  ? (c < 2918
                    ? (c >= 2911 && c <= 2915)
                    : c <= 2927)
                  : (c <= 2929 || (c < 2949
                    ? (c >= 2946 && c <= 2947)
                    : c <= 2954)))))))
            : (c <= 2960 || (c < 3031
              ? (c < 2984
                ? (c < 2972
                  ? (c < 2969
                    ? (c >= 2962 && c <= 2965)
                    : c <= 2970)
                  : (c <= 2972 || (c < 2979
                    ? (c >= 2974 && c <= 2975)
                    : c <= 2980)))
                : (c <= 2986 || (c < 3014
                  ? (c < 3006
                    ? (c >= 2990 && c <= 3001)
                    : c <= 3010)
                  : (c <= 3016 || (c < 3024
                    ? (c >= 3018 && c <= 3021)
                    : c <= 3024)))))
              : (c <= 3031 || (c < 3132
                ? (c < 3086
                  ? (c < 3072
                    ? (c >= 3046 && c <= 3055)
                    : c <= 3084)
                  : (c <= 3088 || (c < 3114
                    ? (c >= 3090 && c <= 3112)
                    : c <= 3129)))
                : (c <= 3140 || (c < 3157
                  ? (c < 3146
                    ? (c >= 3142 && c <= 3144)
                    : c <= 3149)
                  : (c <= 3158 || (c < 3165
                    ? (c >= 3160 && c <= 3162)
                    : c <= 3165)))))))))
          : (c <= 3171 || (c < 3450
            ? (c < 3293
              ? (c < 3242
                ? (c < 3205
                  ? (c < 3200
                    ? (c >= 3174 && c <= 3183)
                    : c <= 3203)
                  : (c <= 3212 || (c < 3218
                    ? (c >= 3214 && c <= 3216)
                    : c <= 3240)))
                : (c <= 3251 || (c < 3270
                  ? (c < 3260
                    ? (c >= 3253 && c <= 3257)
                    : c <= 3268)
                  : (c <= 3272 || (c < 3285
                    ? (c >= 3274 && c <= 3277)
                    : c <= 3286)))))
              : (c <= 3294 || (c < 3346
                ? (c < 3313
                  ? (c < 3302
                    ? (c >= 3296 && c <= 3299)
                    : c <= 3311)
                  : (c <= 3314 || (c < 3342
                    ? (c >= 3328 && c <= 3340)
                    : c <= 3344)))
                : (c <= 3396 || (c < 3412
                  ? (c < 3402
                    ? (c >= 3398 && c <= 3400)
                    : c <= 3406)
                  : (c <= 3415 || (c < 3430
                    ? (c >= 3423 && c <= 3427)
                    : c <= 3439)))))))
            : (c <= 3455 || (c < 3570
              ? (c < 3520
                ? (c < 3482
                  ? (c < 3461
                    ? (c >= 3457 && c <= 3459)
                    : c <= 3478)
                  : (c <= 3505 || (c < 3517
                    ? (c >= 3507 && c <= 3515)
                    : c <= 3517)))
                : (c <= 3526 || (c < 3542
                  ? (c < 3535
                    ? c == 3530
                    : c <= 3540)
                  : (c <= 3542 || (c < 3558
                    ? (c >= 3544 && c <= 3551)
                    : c <= 3567)))))
              : (c <= 3571 || (c < 3718
                ? (c < 3664
                  ? (c < 3648
                    ? (c >= 3585 && c <= 3642)
                    : c <= 3662)
                  : (c <= 3673 || (c < 3716
                    ? (c >= 3713 && c <= 3714)
                    : c <= 3716)))
                : (c <= 3722 || (c < 3751
                  ? (c < 3749
                    ? (c >= 3724 && c <= 3747)
                    : c <= 3749)
                  : (c <= 3773 || (c >= 3776 && c <= 3780)))))))))))))
      : (c <= 3782 || (c < 8025
        ? (c < 5888
          ? (c < 4688
            ? (c < 3953
              ? (c < 3872
                ? (c < 3804
                  ? (c < 3792
                    ? (c >= 3784 && c <= 3789)
                    : c <= 3801)
                  : (c <= 3807 || (c < 3864
                    ? c == 3840
                    : c <= 3865)))
                : (c <= 3881 || (c < 3897
                  ? (c < 3895
                    ? c == 3893
                    : c <= 3895)
                  : (c <= 3897 || (c < 3913
                    ? (c >= 3902 && c <= 3911)
                    : c <= 3948)))))
              : (c <= 3972 || (c < 4256
                ? (c < 4038
                  ? (c < 3993
                    ? (c >= 3974 && c <= 3991)
                    : c <= 4028)
                  : (c <= 4038 || (c < 4176
                    ? (c >= 4096 && c <= 4169)
                    : c <= 4253)))
                : (c <= 4293 || (c < 4304
                  ? (c < 4301
                    ? c == 4295
                    : c <= 4301)
                  : (c <= 4346 || (c < 4682
                    ? (c >= 4348 && c <= 4680)
                    : c <= 4685)))))))
            : (c <= 4694 || (c < 4882
              ? (c < 4786
                ? (c < 4704
                  ? (c < 4698
                    ? c == 4696
                    : c <= 4701)
                  : (c <= 4744 || (c < 4752
                    ? (c >= 4746 && c <= 4749)
                    : c <= 4784)))
                : (c <= 4789 || (c < 4802
                  ? (c < 4800
                    ? (c >= 4792 && c <= 4798)
                    : c <= 4800)
                  : (c <= 4805 || (c < 4824
                    ? (c >= 4808 && c <= 4822)
                    : c <= 4880)))))
              : (c <= 4885 || (c < 5112
                ? (c < 4969
                  ? (c < 4957
                    ? (c >= 4888 && c <= 4954)
                    : c <= 4959)
                  : (c <= 4977 || (c < 5024
                    ? (c >= 4992 && c <= 5007)
                    : c <= 5109)))
                : (c <= 5117 || (c < 5761
                  ? (c < 5743
                    ? (c >= 5121 && c <= 5740)
                    : c <= 5759)
                  : (c <= 5786 || (c < 5870
                    ? (c >= 5792 && c <= 5866)
                    : c <= 5880)))))))))
          : (c <= 5909 || (c < 6688
            ? (c < 6176
              ? (c < 6016
                ? (c < 5984
                  ? (c < 5952
                    ? (c >= 5919 && c <= 5940)
                    : c <= 5971)
                  : (c <= 5996 || (c < 6002
                    ? (c >= 5998 && c <= 6000)
                    : c <= 6003)))
                : (c <= 6099 || (c < 6112
                  ? (c < 6108
                    ? c == 6103
                    : c <= 6109)
                  : (c <= 6121 || (c < 6159
                    ? (c >= 6155 && c <= 6157)
                    : c <= 6169)))))
              : (c <= 6264 || (c < 6470
                ? (c < 6400
                  ? (c < 6320
                    ? (c >= 6272 && c <= 6314)
                    : c <= 6389)
                  : (c <= 6430 || (c < 6448
                    ? (c >= 6432 && c <= 6443)
                    : c <= 6459)))
                : (c <= 6509 || (c < 6576
                  ? (c < 6528
                    ? (c >= 6512 && c <= 6516)
                    : c <= 6571)
                  : (c <= 6601 || (c < 6656
                    ? (c >= 6608 && c <= 6618)
                    : c <= 6683)))))))
            : (c <= 6750 || (c < 7232
              ? (c < 6847
                ? (c < 6800
                  ? (c < 6783
                    ? (c >= 6752 && c <= 6780)
                    : c <= 6793)
                  : (c <= 6809 || (c < 6832
                    ? c == 6823
                    : c <= 6845)))
                : (c <= 6862 || (c < 7019
                  ? (c < 6992
                    ? (c >= 6912 && c <= 6988)
                    : c <= 7001)
                  : (c <= 7027 || (c < 7168
                    ? (c >= 7040 && c <= 7155)
                    : c <= 7223)))))
              : (c <= 7241 || (c < 7380
                ? (c < 7312
                  ? (c < 7296
                    ? (c >= 7245 && c <= 7293)
                    : c <= 7304)
                  : (c <= 7354 || (c < 7376
                    ? (c >= 7357 && c <= 7359)
                    : c <= 7378)))
                : (c <= 7418 || (c < 7968
                  ? (c < 7960
                    ? (c >= 7424 && c <= 7957)
                    : c <= 7965)
                  : (c <= 8005 || (c < 8016
                    ? (c >= 8008 && c <= 8013)
                    : c <= 8023)))))))))))
        : (c <= 8025 || (c < 11720
          ? (c < 8458
            ? (c < 8178
              ? (c < 8126
                ? (c < 8031
                  ? (c < 8029
                    ? c == 8027
                    : c <= 8029)
                  : (c <= 8061 || (c < 8118
                    ? (c >= 8064 && c <= 8116)
                    : c <= 8124)))
                : (c <= 8126 || (c < 8144
                  ? (c < 8134
                    ? (c >= 8130 && c <= 8132)
                    : c <= 8140)
                  : (c <= 8147 || (c < 8160
                    ? (c >= 8150 && c <= 8155)
                    : c <= 8172)))))
              : (c <= 8180 || (c < 8336
                ? (c < 8276
                  ? (c < 8255
                    ? (c >= 8182 && c <= 8188)
                    : c <= 8256)
                  : (c <= 8276 || (c < 8319
                    ? c == 8305
                    : c <= 8319)))
                : (c <= 8348 || (c < 8421
                  ? (c < 8417
                    ? (c >= 8400 && c <= 8412)
                    : c <= 8417)
                  : (c <= 8432 || (c < 8455
                    ? c == 8450
                    : c <= 8455)))))))
            : (c <= 8467 || (c < 11499
              ? (c < 8490
                ? (c < 8484
                  ? (c < 8472
                    ? c == 8469
                    : c <= 8477)
                  : (c <= 8484 || (c < 8488
                    ? c == 8486
                    : c <= 8488)))
                : (c <= 8505 || (c < 8526
                  ? (c < 8517
                    ? (c >= 8508 && c <= 8511)
                    : c <= 8521)
                  : (c <= 8526 || (c < 11264
                    ? (c >= 8544 && c <= 8584)
                    : c <= 11492)))))
              : (c <= 11507 || (c < 11647
                ? (c < 11565
                  ? (c < 11559
                    ? (c >= 11520 && c <= 11557)
                    : c <= 11559)
                  : (c <= 11565 || (c < 11631
                    ? (c >= 11568 && c <= 11623)
                    : c <= 11631)))
                : (c <= 11670 || (c < 11696
                  ? (c < 11688
                    ? (c >= 11680 && c <= 11686)
                    : c <= 11694)
                  : (c <= 11702 || (c < 11712
                    ? (c >= 11704 && c <= 11710)
                    : c <= 11718)))))))))
          : (c <= 11726 || (c < 42623
            ? (c < 12540
              ? (c < 12337
                ? (c < 11744
                  ? (c < 11736
                    ? (c >= 11728 && c <= 11734)
                    : c <= 11742)
                  : (c <= 11775 || (c < 12321
                    ? (c >= 12293 && c <= 12295)
                    : c <= 12335)))
                : (c <= 12341 || (c < 12441
                  ? (c < 12353
                    ? (c >= 12344 && c <= 12348)
                    : c <= 12438)
                  : (c <= 12442 || (c < 12449
                    ? (c >= 12445 && c <= 12447)
                    : c <= 12538)))))
              : (c <= 12543 || (c < 19968
                ? (c < 12704
                  ? (c < 12593
                    ? (c >= 12549 && c <= 12591)
                    : c <= 12686)
                  : (c <= 12735 || (c < 13312
                    ? (c >= 12784 && c <= 12799)
                    : c <= 19903)))
                : (c <= 42124 || (c < 42512
                  ? (c < 42240
                    ? (c >= 42192 && c <= 42237)
                    : c <= 42508)
                  : (c <= 42539 || (c < 42612
                    ? (c >= 42560 && c <= 42607)
                    : c <= 42621)))))))
            : (c <= 42737 || (c < 43232
              ? (c < 42965
                ? (c < 42891
                  ? (c < 42786
                    ? (c >= 42775 && c <= 42783)
                    : c <= 42888)
                  : (c <= 42954 || (c < 42963
                    ? (c >= 42960 && c <= 42961)
                    : c <= 42963)))
                : (c <= 42969 || (c < 43072
                  ? (c < 43052
                    ? (c >= 42994 && c <= 43047)
                    : c <= 43052)
                  : (c <= 43123 || (c < 43216
                    ? (c >= 43136 && c <= 43205)
                    : c <= 43225)))))
              : (c <= 43255 || (c < 43471
                ? (c < 43312
                  ? (c < 43261
                    ? c == 43259
                    : c <= 43309)
                  : (c <= 43347 || (c < 43392
                    ? (c >= 43360 && c <= 43388)
                    : c <= 43456)))
                : (c <= 43481 || (c < 43584
                  ? (c < 43520
                    ? (c >= 43488 && c <= 43518)
                    : c <= 43574)
                  : (c <= 43597 || (c >= 43600 && c <= 43609)))))))))))))))
    : (c <= 43638 || (c < 71453
      ? (c < 67639
        ? (c < 65345
          ? (c < 64312
            ? (c < 43888
              ? (c < 43785
                ? (c < 43744
                  ? (c < 43739
                    ? (c >= 43642 && c <= 43714)
                    : c <= 43741)
                  : (c <= 43759 || (c < 43777
                    ? (c >= 43762 && c <= 43766)
                    : c <= 43782)))
                : (c <= 43790 || (c < 43816
                  ? (c < 43808
                    ? (c >= 43793 && c <= 43798)
                    : c <= 43814)
                  : (c <= 43822 || (c < 43868
                    ? (c >= 43824 && c <= 43866)
                    : c <= 43881)))))
              : (c <= 44010 || (c < 63744
                ? (c < 44032
                  ? (c < 44016
                    ? (c >= 44012 && c <= 44013)
                    : c <= 44025)
                  : (c <= 55203 || (c < 55243
                    ? (c >= 55216 && c <= 55238)
                    : c <= 55291)))
                : (c <= 64109 || (c < 64275
                  ? (c < 64256
                    ? (c >= 64112 && c <= 64217)
                    : c <= 64262)
                  : (c <= 64279 || (c < 64298
                    ? (c >= 64285 && c <= 64296)
                    : c <= 64310)))))))
            : (c <= 64316 || (c < 65075
              ? (c < 64612
                ? (c < 64323
                  ? (c < 64320
                    ? c == 64318
                    : c <= 64321)
                  : (c <= 64324 || (c < 64467
                    ? (c >= 64326 && c <= 64433)
                    : c <= 64605)))
                : (c <= 64829 || (c < 65008
                  ? (c < 64914
                    ? (c >= 64848 && c <= 64911)
                    : c <= 64967)
                  : (c <= 65017 || (c < 65056
                    ? (c >= 65024 && c <= 65039)
                    : c <= 65071)))))
              : (c <= 65076 || (c < 65147
                ? (c < 65139
                  ? (c < 65137
                    ? (c >= 65101 && c <= 65103)
                    : c <= 65137)
                  : (c <= 65139 || (c < 65145
                    ? c == 65143
                    : c <= 65145)))
                : (c <= 65147 || (c < 65296
                  ? (c < 65151
                    ? c == 65149
                    : c <= 65276)
                  : (c <= 65305 || (c < 65343
                    ? (c >= 65313 && c <= 65338)
                    : c <= 65343)))))))))
          : (c <= 65370 || (c < 66513
            ? (c < 65664
              ? (c < 65536
                ? (c < 65482
                  ? (c < 65474
                    ? (c >= 65382 && c <= 65470)
                    : c <= 65479)
                  : (c <= 65487 || (c < 65498
                    ? (c >= 65490 && c <= 65495)
                    : c <= 65500)))
                : (c <= 65547 || (c < 65596
                  ? (c < 65576
                    ? (c >= 65549 && c <= 65574)
                    : c <= 65594)
                  : (c <= 65597 || (c < 65616
                    ? (c >= 65599 && c <= 65613)
                    : c <= 65629)))))
              : (c <= 65786 || (c < 66304
                ? (c < 66176
                  ? (c < 66045
                    ? (c >= 65856 && c <= 65908)
                    : c <= 66045)
                  : (c <= 66204 || (c < 66272
                    ? (c >= 66208 && c <= 66256)
                    : c <= 66272)))
                : (c <= 66335 || (c < 66432
                  ? (c < 66384
                    ? (c >= 66349 && c <= 66378)
                    : c <= 66426)
                  : (c <= 66461 || (c < 66504
                    ? (c >= 66464 && c <= 66499)
                    : c <= 66511)))))))
            : (c <= 66517 || (c < 66979
              ? (c < 66864
                ? (c < 66736
                  ? (c < 66720
                    ? (c >= 66560 && c <= 66717)
                    : c <= 66729)
                  : (c <= 66771 || (c < 66816
                    ? (c >= 66776 && c <= 66811)
                    : c <= 66855)))
                : (c <= 66915 || (c < 66956
                  ? (c < 66940
                    ? (c >= 66928 && c <= 66938)
                    : c <= 66954)
                  : (c <= 66962 || (c < 66967
                    ? (c >= 66964 && c <= 66965)
                    : c <= 66977)))))
              : (c <= 66993 || (c < 67456
                ? (c < 67072
                  ? (c < 67003
                    ? (c >= 66995 && c <= 67001)
                    : c <= 67004)
                  : (c <= 67382 || (c < 67424
                    ? (c >= 67392 && c <= 67413)
                    : c <= 67431)))
                : (c <= 67461 || (c < 67584
                  ? (c < 67506
                    ? (c >= 67463 && c <= 67504)
                    : c <= 67514)
                  : (c <= 67589 || (c < 67594
                    ? c == 67592
                    : c <= 67637)))))))))))
        : (c <= 67640 || (c < 69956
          ? (c < 68448
            ? (c < 68101
              ? (c < 67828
                ? (c < 67680
                  ? (c < 67647
                    ? c == 67644
                    : c <= 67669)
                  : (c <= 67702 || (c < 67808
                    ? (c >= 67712 && c <= 67742)
                    : c <= 67826)))
                : (c <= 67829 || (c < 67968
                  ? (c < 67872
                    ? (c >= 67840 && c <= 67861)
                    : c <= 67897)
                  : (c <= 68023 || (c < 68096
                    ? (c >= 68030 && c <= 68031)
                    : c <= 68099)))))
              : (c <= 68102 || (c < 68192
                ? (c < 68121
                  ? (c < 68117
                    ? (c >= 68108 && c <= 68115)
                    : c <= 68119)
                  : (c <= 68149 || (c < 68159
                    ? (c >= 68152 && c <= 68154)
                    : c <= 68159)))
                : (c <= 68220 || (c < 68297
                  ? (c < 68288
                    ? (c >= 68224 && c <= 68252)
                    : c <= 68295)
                  : (c <= 68326 || (c < 68416
                    ? (c >= 68352 && c <= 68405)
                    : c <= 68437)))))))
            : (c <= 68466 || (c < 69424
              ? (c < 68912
                ? (c < 68736
                  ? (c < 68608
                    ? (c >= 68480 && c <= 68497)
                    : c <= 68680)
                  : (c <= 68786 || (c < 68864
                    ? (c >= 68800 && c <= 68850)
                    : c <= 68903)))
                : (c <= 68921 || (c < 69296
                  ? (c < 69291
                    ? (c >= 69248 && c <= 69289)
                    : c <= 69292)
                  : (c <= 69297 || (c < 69415
                    ? (c >= 69376 && c <= 69404)
                    : c <= 69415)))))
              : (c <= 69456 || (c < 69759
                ? (c < 69600
                  ? (c < 69552
                    ? (c >= 69488 && c <= 69509)
                    : c <= 69572)
                  : (c <= 69622 || (c < 69734
                    ? (c >= 69632 && c <= 69702)
                    : c <= 69749)))
                : (c <= 69818 || (c < 69872
                  ? (c < 69840
                    ? c == 69826
                    : c <= 69864)
                  : (c <= 69881 || (c < 69942
                    ? (c >= 69888 && c <= 69940)
                    : c <= 69951)))))))))
          : (c <= 69959 || (c < 70459
            ? (c < 70282
              ? (c < 70108
                ? (c < 70016
                  ? (c < 70006
                    ? (c >= 69968 && c <= 70003)
                    : c <= 70006)
                  : (c <= 70084 || (c < 70094
                    ? (c >= 70089 && c <= 70092)
                    : c <= 70106)))
                : (c <= 70108 || (c < 70206
                  ? (c < 70163
                    ? (c >= 70144 && c <= 70161)
                    : c <= 70199)
                  : (c <= 70206 || (c < 70280
                    ? (c >= 70272 && c <= 70278)
                    : c <= 70280)))))
              : (c <= 70285 || (c < 70405
                ? (c < 70320
                  ? (c < 70303
                    ? (c >= 70287 && c <= 70301)
                    : c <= 70312)
                  : (c <= 70378 || (c < 70400
                    ? (c >= 70384 && c <= 70393)
                    : c <= 70403)))
                : (c <= 70412 || (c < 70442
                  ? (c < 70419
                    ? (c >= 70415 && c <= 70416)
                    : c <= 70440)
                  : (c <= 70448 || (c < 70453
                    ? (c >= 70450 && c <= 70451)
                    : c <= 70457)))))))
            : (c <= 70468 || (c < 70855
              ? (c < 70502
                ? (c < 70480
                  ? (c < 70475
                    ? (c >= 70471 && c <= 70472)
                    : c <= 70477)
                  : (c <= 70480 || (c < 70493
                    ? c == 70487
                    : c <= 70499)))
                : (c <= 70508 || (c < 70736
                  ? (c < 70656
                    ? (c >= 70512 && c <= 70516)
                    : c <= 70730)
                  : (c <= 70745 || (c < 70784
                    ? (c >= 70750 && c <= 70753)
                    : c <= 70853)))))
              : (c <= 70855 || (c < 71236
                ? (c < 71096
                  ? (c < 71040
                    ? (c >= 70864 && c <= 70873)
                    : c <= 71093)
                  : (c <= 71104 || (c < 71168
                    ? (c >= 71128 && c <= 71133)
                    : c <= 71232)))
                : (c <= 71236 || (c < 71360
                  ? (c < 71296
                    ? (c >= 71248 && c <= 71257)
                    : c <= 71352)
                  : (c <= 71369 || (c >= 71424 && c <= 71450)))))))))))))
      : (c <= 71467 || (c < 119973
        ? (c < 77824
          ? (c < 72760
            ? (c < 72016
              ? (c < 71945
                ? (c < 71680
                  ? (c < 71488
                    ? (c >= 71472 && c <= 71481)
                    : c <= 71494)
                  : (c <= 71738 || (c < 71935
                    ? (c >= 71840 && c <= 71913)
                    : c <= 71942)))
                : (c <= 71945 || (c < 71960
                  ? (c < 71957
                    ? (c >= 71948 && c <= 71955)
                    : c <= 71958)
                  : (c <= 71989 || (c < 71995
                    ? (c >= 71991 && c <= 71992)
                    : c <= 72003)))))
              : (c <= 72025 || (c < 72263
                ? (c < 72154
                  ? (c < 72106
                    ? (c >= 72096 && c <= 72103)
                    : c <= 72151)
                  : (c <= 72161 || (c < 72192
                    ? (c >= 72163 && c <= 72164)
                    : c <= 72254)))
                : (c <= 72263 || (c < 72368
                  ? (c < 72349
                    ? (c >= 72272 && c <= 72345)
                    : c <= 72349)
                  : (c <= 72440 || (c < 72714
                    ? (c >= 72704 && c <= 72712)
                    : c <= 72758)))))))
            : (c <= 72768 || (c < 73056
              ? (c < 72968
                ? (c < 72850
                  ? (c < 72818
                    ? (c >= 72784 && c <= 72793)
                    : c <= 72847)
                  : (c <= 72871 || (c < 72960
                    ? (c >= 72873 && c <= 72886)
                    : c <= 72966)))
                : (c <= 72969 || (c < 73020
                  ? (c < 73018
                    ? (c >= 72971 && c <= 73014)
                    : c <= 73018)
                  : (c <= 73021 || (c < 73040
                    ? (c >= 73023 && c <= 73031)
                    : c <= 73049)))))
              : (c <= 73061 || (c < 73440
                ? (c < 73104
                  ? (c < 73066
                    ? (c >= 73063 && c <= 73064)
                    : c <= 73102)
                  : (c <= 73105 || (c < 73120
                    ? (c >= 73107 && c <= 73112)
                    : c <= 73129)))
                : (c <= 73462 || (c < 74752
                  ? (c < 73728
                    ? c == 73648
                    : c <= 74649)
                  : (c <= 74862 || (c < 77712
                    ? (c >= 74880 && c <= 75075)
                    : c <= 77808)))))))))
          : (c <= 78894 || (c < 110576
            ? (c < 93027
              ? (c < 92864
                ? (c < 92736
                  ? (c < 92160
                    ? (c >= 82944 && c <= 83526)
                    : c <= 92728)
                  : (c <= 92766 || (c < 92784
                    ? (c >= 92768 && c <= 92777)
                    : c <= 92862)))
                : (c <= 92873 || (c < 92928
                  ? (c < 92912
                    ? (c >= 92880 && c <= 92909)
                    : c <= 92916)
                  : (c <= 92982 || (c < 93008
                    ? (c >= 92992 && c <= 92995)
                    : c <= 93017)))))
              : (c <= 93047 || (c < 94176
                ? (c < 93952
                  ? (c < 93760
                    ? (c >= 93053 && c <= 93071)
                    : c <= 93823)
                  : (c <= 94026 || (c < 94095
                    ? (c >= 94031 && c <= 94087)
                    : c <= 94111)))
                : (c <= 94177 || (c < 94208
                  ? (c < 94192
                    ? (c >= 94179 && c <= 94180)
                    : c <= 94193)
                  : (c <= 100343 || (c < 101632
                    ? (c >= 100352 && c <= 101589)
                    : c <= 101640)))))))
            : (c <= 110579 || (c < 118528
              ? (c < 110960
                ? (c < 110592
                  ? (c < 110589
                    ? (c >= 110581 && c <= 110587)
                    : c <= 110590)
                  : (c <= 110882 || (c < 110948
                    ? (c >= 110928 && c <= 110930)
                    : c <= 110951)))
                : (c <= 111355 || (c < 113792
                  ? (c < 113776
                    ? (c >= 113664 && c <= 113770)
                    : c <= 113788)
                  : (c <= 113800 || (c < 113821
                    ? (c >= 113808 && c <= 113817)
                    : c <= 113822)))))
              : (c <= 118573 || (c < 119210
                ? (c < 119149
                  ? (c < 119141
                    ? (c >= 118576 && c <= 118598)
                    : c <= 119145)
                  : (c <= 119154 || (c < 119173
                    ? (c >= 119163 && c <= 119170)
                    : c <= 119179)))
                : (c <= 119213 || (c < 119894
                  ? (c < 119808
                    ? (c >= 119362 && c <= 119364)
                    : c <= 119892)
                  : (c <= 119964 || (c < 119970
                    ? (c >= 119966 && c <= 119967)
                    : c <= 119970)))))))))))
        : (c <= 119974 || (c < 124912
          ? (c < 120746
            ? (c < 120134
              ? (c < 120071
                ? (c < 119995
                  ? (c < 119982
                    ? (c >= 119977 && c <= 119980)
                    : c <= 119993)
                  : (c <= 119995 || (c < 120005
                    ? (c >= 119997 && c <= 120003)
                    : c <= 120069)))
                : (c <= 120074 || (c < 120094
                  ? (c < 120086
                    ? (c >= 120077 && c <= 120084)
                    : c <= 120092)
                  : (c <= 120121 || (c < 120128
                    ? (c >= 120123 && c <= 120126)
                    : c <= 120132)))))
              : (c <= 120134 || (c < 120572
                ? (c < 120488
                  ? (c < 120146
                    ? (c >= 120138 && c <= 120144)
                    : c <= 120485)
                  : (c <= 120512 || (c < 120540
                    ? (c >= 120514 && c <= 120538)
                    : c <= 120570)))
                : (c <= 120596 || (c < 120656
                  ? (c < 120630
                    ? (c >= 120598 && c <= 120628)
                    : c <= 120654)
                  : (c <= 120686 || (c < 120714
                    ? (c >= 120688 && c <= 120712)
                    : c <= 120744)))))))
            : (c <= 120770 || (c < 122907
              ? (c < 121476
                ? (c < 121344
                  ? (c < 120782
                    ? (c >= 120772 && c <= 120779)
                    : c <= 120831)
                  : (c <= 121398 || (c < 121461
                    ? (c >= 121403 && c <= 121452)
                    : c <= 121461)))
                : (c <= 121476 || (c < 122624
                  ? (c < 121505
                    ? (c >= 121499 && c <= 121503)
                    : c <= 121519)
                  : (c <= 122654 || (c < 122888
                    ? (c >= 122880 && c <= 122886)
                    : c <= 122904)))))
              : (c <= 122913 || (c < 123214
                ? (c < 123136
                  ? (c < 122918
                    ? (c >= 122915 && c <= 122916)
                    : c <= 122922)
                  : (c <= 123180 || (c < 123200
                    ? (c >= 123184 && c <= 123197)
                    : c <= 123209)))
                : (c <= 123214 || (c < 124896
                  ? (c < 123584
                    ? (c >= 123536 && c <= 123566)
                    : c <= 123641)
                  : (c <= 124902 || (c < 124909
                    ? (c >= 124904 && c <= 124907)
                    : c <= 124910)))))))))
          : (c <= 124926 || (c < 126557
            ? (c < 126521
              ? (c < 126469
                ? (c < 125184
                  ? (c < 125136
                    ? (c >= 124928 && c <= 125124)
                    : c <= 125142)
                  : (c <= 125259 || (c < 126464
                    ? (c >= 125264 && c <= 125273)
                    : c <= 126467)))
                : (c <= 126495 || (c < 126503
                  ? (c < 126500
                    ? (c >= 126497 && c <= 126498)
                    : c <= 126500)
                  : (c <= 126503 || (c < 126516
                    ? (c >= 126505 && c <= 126514)
                    : c <= 126519)))))
              : (c <= 126521 || (c < 126541
                ? (c < 126535
                  ? (c < 126530
                    ? c == 126523
                    : c <= 126530)
                  : (c <= 126535 || (c < 126539
                    ? c == 126537
                    : c <= 126539)))
                : (c <= 126543 || (c < 126551
                  ? (c < 126548
                    ? (c >= 126545 && c <= 126546)
                    : c <= 126548)
                  : (c <= 126551 || (c < 126555
                    ? c == 126553
                    : c <= 126555)))))))
            : (c <= 126557 || (c < 126629
              ? (c < 126580
                ? (c < 126564
                  ? (c < 126561
                    ? c == 126559
                    : c <= 126562)
                  : (c <= 126564 || (c < 126572
                    ? (c >= 126567 && c <= 126570)
                    : c <= 126578)))
                : (c <= 126583 || (c < 126592
                  ? (c < 126590
                    ? (c >= 126585 && c <= 126588)
                    : c <= 126590)
                  : (c <= 126601 || (c < 126625
                    ? (c >= 126603 && c <= 126619)
                    : c <= 126627)))))
              : (c <= 126633 || (c < 178208
                ? (c < 131072
                  ? (c < 130032
                    ? (c >= 126635 && c <= 126651)
                    : c <= 130041)
                  : (c <= 173791 || (c < 177984
                    ? (c >= 173824 && c <= 177976)
                    : c <= 178205)))
                : (c <= 183969 || (c < 196608
                  ? (c < 194560
                    ? (c >= 183984 && c <= 191456)
                    : c <= 195101)
                  : (c <= 201546 || (c >= 917760 && c <= 917999)))))))))))))))));
}

static bool ts_lex(TSLexer *lexer, TSStateId state) {
  START_LEXER();
  eof = lexer->eof(lexer);
  switch (state) {
    case 0:
      if (eof) ADVANCE(169);
      if (lookahead == '#') ADVANCE(199);
      if (lookahead == '*') ADVANCE(177);
      if (lookahead == ',') ADVANCE(182);
      if (lookahead == '0') ADVANCE(213);
      if (lookahead == ':') ADVANCE(171);
      if (lookahead == 'F') ADVANCE(13);
      if (lookahead == 'T') ADVANCE(114);
      if (lookahead == '\\') SKIP(165)
      if (lookahead == 'a') ADVANCE(123);
      if (lookahead == 'c') ADVANCE(14);
      if (lookahead == 'f') ADVANCE(118);
      if (lookahead == 'i') ADVANCE(92);
      if (lookahead == 'l') ADVANCE(98);
      if (lookahead == 'm') ADVANCE(38);
      if (lookahead == 'n') ADVANCE(99);
      if (lookahead == 'o') ADVANCE(140);
      if (lookahead == 'p') ADVANCE(19);
      if (lookahead == 'r') ADVANCE(51);
      if (lookahead == 's') ADVANCE(30);
      if (lookahead == 't') ADVANCE(65);
      if (lookahead == 'u') ADVANCE(127);
      if (lookahead == 'w') ADVANCE(70);
      if (lookahead == '{') ADVANCE(149);
      if (lookahead == '}') ADVANCE(150);
      if (lookahead == '\t' ||
          lookahead == '\n' ||
          lookahead == '\f' ||
          lookahead == '\r' ||
          lookahead == ' ' ||
          lookahead == 8203 ||
          lookahead == 8288 ||
          lookahead == 65279) SKIP(0)
      if (('1' <= lookahead && lookahead <= '9')) ADVANCE(209);
      END_STATE();
    case 1:
      if (lookahead == '\n') ADVANCE(204);
      END_STATE();
    case 2:
      if (lookahead == '\n') SKIP(5)
      END_STATE();
    case 3:
      if (lookahead == '\n') SKIP(5)
      if (lookahead == '\r') SKIP(2)
      END_STATE();
    case 4:
      if (lookahead == '#') ADVANCE(199);
      if (lookahead == '\\') ADVANCE(205);
      if (lookahead == '{') ADVANCE(149);
      if (lookahead == '}') ADVANCE(150);
      if (lookahead == '\t' ||
          lookahead == '\n' ||
          lookahead == '\f' ||
          lookahead == '\r' ||
          lookahead == ' ' ||
          lookahead == 8203 ||
          lookahead == 8288 ||
          lookahead == 65279) SKIP(4)
      END_STATE();
    case 5:
      if (lookahead == '#') ADVANCE(199);
      if (lookahead == '\\') SKIP(3)
      if (lookahead == '\t' ||
          lookahead == '\n' ||
          lookahead == '\f' ||
          lookahead == '\r' ||
          lookahead == ' ' ||
          lookahead == 8203 ||
          lookahead == 8288 ||
          lookahead == 65279) SKIP(5)
      if (sym_identifier_character_set_1(lookahead)) ADVANCE(200);
      END_STATE();
    case 6:
      if (lookahead == ':') ADVANCE(172);
      END_STATE();
    case 7:
      if (lookahead == ':') ADVANCE(173);
      END_STATE();
    case 8:
      if (lookahead == '_') ADVANCE(151);
      if (lookahead == '0' ||
          lookahead == '1') ADVANCE(210);
      END_STATE();
    case 9:
      if (lookahead == '_') ADVANCE(152);
      if (('0' <= lookahead && lookahead <= '7')) ADVANCE(211);
      END_STATE();
    case 10:
      if (lookahead == '_') ADVANCE(155);
      if (('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'F') ||
          ('a' <= lookahead && lookahead <= 'f')) ADVANCE(212);
      END_STATE();
    case 11:
      if (lookahead == '_') ADVANCE(148);
      END_STATE();
    case 12:
      if (lookahead == '_') ADVANCE(147);
      END_STATE();
    case 13:
      if (lookahead == 'a') ADVANCE(89);
      END_STATE();
    case 14:
      if (lookahead == 'a') ADVANCE(27);
      if (lookahead == 'o') ADVANCE(93);
      END_STATE();
    case 15:
      if (lookahead == 'a') ADVANCE(80);
      END_STATE();
    case 16:
      if (lookahead == 'a') ADVANCE(11);
      END_STATE();
    case 17:
      if (lookahead == 'a') ADVANCE(190);
      END_STATE();
    case 18:
      if (lookahead == 'a') ADVANCE(91);
      END_STATE();
    case 19:
      if (lookahead == 'a') ADVANCE(119);
      END_STATE();
    case 20:
      if (lookahead == 'a') ADVANCE(62);
      END_STATE();
    case 21:
      if (lookahead == 'a') ADVANCE(69);
      END_STATE();
    case 22:
      if (lookahead == 'a') ADVANCE(35);
      END_STATE();
    case 23:
      if (lookahead == 'a') ADVANCE(108);
      END_STATE();
    case 24:
      if (lookahead == 'a') ADVANCE(85);
      END_STATE();
    case 25:
      if (lookahead == 'a') ADVANCE(138);
      END_STATE();
    case 26:
      if (lookahead == 'b') ADVANCE(106);
      END_STATE();
    case 27:
      if (lookahead == 'c') ADVANCE(66);
      END_STATE();
    case 28:
      if (lookahead == 'c') ADVANCE(82);
      END_STATE();
    case 29:
      if (lookahead == 'c') ADVANCE(82);
      if (lookahead == 'p') ADVANCE(143);
      END_STATE();
    case 30:
      if (lookahead == 'c') ADVANCE(122);
      if (lookahead == 'h') ADVANCE(52);
      if (lookahead == 'k') ADVANCE(68);
      if (lookahead == 'n') ADVANCE(15);
      END_STATE();
    case 31:
      if (lookahead == 'c') ADVANCE(53);
      END_STATE();
    case 32:
      if (lookahead == 'd') ADVANCE(146);
      END_STATE();
    case 33:
      if (lookahead == 'd') ADVANCE(75);
      END_STATE();
    case 34:
      if (lookahead == 'd') ADVANCE(17);
      if (lookahead == 'f') ADVANCE(67);
      if (lookahead == 't') ADVANCE(21);
      END_STATE();
    case 35:
      if (lookahead == 'd') ADVANCE(125);
      END_STATE();
    case 36:
      if (lookahead == 'd') ADVANCE(25);
      END_STATE();
    case 37:
      if (lookahead == 'd') ADVANCE(47);
      END_STATE();
    case 38:
      if (lookahead == 'e') ADVANCE(128);
      if (lookahead == 'o') ADVANCE(32);
      END_STATE();
    case 39:
      if (lookahead == 'e') ADVANCE(176);
      END_STATE();
    case 40:
      if (lookahead == 'e') ADVANCE(206);
      END_STATE();
    case 41:
      if (lookahead == 'e') ADVANCE(26);
      END_STATE();
    case 42:
      if (lookahead == 'e') ADVANCE(170);
      END_STATE();
    case 43:
      if (lookahead == 'e') ADVANCE(207);
      END_STATE();
    case 44:
      if (lookahead == 'e') ADVANCE(186);
      END_STATE();
    case 45:
      if (lookahead == 'e') ADVANCE(57);
      END_STATE();
    case 46:
      if (lookahead == 'e') ADVANCE(174);
      END_STATE();
    case 47:
      if (lookahead == 'e') ADVANCE(6);
      END_STATE();
    case 48:
      if (lookahead == 'e') ADVANCE(187);
      END_STATE();
    case 49:
      if (lookahead == 'e') ADVANCE(195);
      END_STATE();
    case 50:
      if (lookahead == 'e') ADVANCE(175);
      END_STATE();
    case 51:
      if (lookahead == 'e') ADVANCE(129);
      if (lookahead == 'u') ADVANCE(84);
      END_STATE();
    case 52:
      if (lookahead == 'e') ADVANCE(83);
      END_STATE();
    case 53:
      if (lookahead == 'e') ADVANCE(126);
      END_STATE();
    case 54:
      if (lookahead == 'e') ADVANCE(22);
      END_STATE();
    case 55:
      if (lookahead == 'e') ADVANCE(112);
      END_STATE();
    case 56:
      if (lookahead == 'e') ADVANCE(113);
      END_STATE();
    case 57:
      if (lookahead == 'f') ADVANCE(76);
      END_STATE();
    case 58:
      if (lookahead == 'f') ADVANCE(72);
      if (lookahead == 't') ADVANCE(21);
      END_STATE();
    case 59:
      if (lookahead == 'f') ADVANCE(77);
      END_STATE();
    case 60:
      if (lookahead == 'g') ADVANCE(185);
      END_STATE();
    case 61:
      if (lookahead == 'g') ADVANCE(197);
      END_STATE();
    case 62:
      if (lookahead == 'g') ADVANCE(48);
      END_STATE();
    case 63:
      if (lookahead == 'g') ADVANCE(59);
      END_STATE();
    case 64:
      if (lookahead == 'h') ADVANCE(180);
      END_STATE();
    case 65:
      if (lookahead == 'h') ADVANCE(120);
      END_STATE();
    case 66:
      if (lookahead == 'h') ADVANCE(44);
      END_STATE();
    case 67:
      if (lookahead == 'i') ADVANCE(61);
      END_STATE();
    case 68:
      if (lookahead == 'i') ADVANCE(107);
      END_STATE();
    case 69:
      if (lookahead == 'i') ADVANCE(97);
      END_STATE();
    case 70:
      if (lookahead == 'i') ADVANCE(136);
      if (lookahead == 'o') ADVANCE(116);
      END_STATE();
    case 71:
      if (lookahead == 'i') ADVANCE(109);
      END_STATE();
    case 72:
      if (lookahead == 'i') ADVANCE(63);
      END_STATE();
    case 73:
      if (lookahead == 'i') ADVANCE(36);
      END_STATE();
    case 74:
      if (lookahead == 'i') ADVANCE(102);
      END_STATE();
    case 75:
      if (lookahead == 'i') ADVANCE(117);
      END_STATE();
    case 76:
      if (lookahead == 'i') ADVANCE(87);
      END_STATE();
    case 77:
      if (lookahead == 'i') ADVANCE(88);
      END_STATE();
    case 78:
      if (lookahead == 'k') ADVANCE(194);
      END_STATE();
    case 79:
      if (lookahead == 'k') ADVANCE(33);
      END_STATE();
    case 80:
      if (lookahead == 'k') ADVANCE(45);
      END_STATE();
    case 81:
      if (lookahead == 'l') ADVANCE(192);
      END_STATE();
    case 82:
      if (lookahead == 'l') ADVANCE(141);
      END_STATE();
    case 83:
      if (lookahead == 'l') ADVANCE(81);
      END_STATE();
    case 84:
      if (lookahead == 'l') ADVANCE(42);
      END_STATE();
    case 85:
      if (lookahead == 'l') ADVANCE(73);
      END_STATE();
    case 86:
      if (lookahead == 'l') ADVANCE(46);
      END_STATE();
    case 87:
      if (lookahead == 'l') ADVANCE(49);
      END_STATE();
    case 88:
      if (lookahead == 'l') ADVANCE(50);
      END_STATE();
    case 89:
      if (lookahead == 'l') ADVANCE(130);
      END_STATE();
    case 90:
      if (lookahead == 'm') ADVANCE(178);
      END_STATE();
    case 91:
      if (lookahead == 'm') ADVANCE(124);
      END_STATE();
    case 92:
      if (lookahead == 'n') ADVANCE(29);
      END_STATE();
    case 93:
      if (lookahead == 'n') ADVANCE(34);
      END_STATE();
    case 94:
      if (lookahead == 'n') ADVANCE(198);
      END_STATE();
    case 95:
      if (lookahead == 'n') ADVANCE(58);
      END_STATE();
    case 96:
      if (lookahead == 'n') ADVANCE(28);
      END_STATE();
    case 97:
      if (lookahead == 'n') ADVANCE(55);
      END_STATE();
    case 98:
      if (lookahead == 'o') ADVANCE(60);
      END_STATE();
    case 99:
      if (lookahead == 'o') ADVANCE(137);
      END_STATE();
    case 100:
      if (lookahead == 'o') ADVANCE(90);
      END_STATE();
    case 101:
      if (lookahead == 'o') ADVANCE(78);
      END_STATE();
    case 102:
      if (lookahead == 'o') ADVANCE(94);
      END_STATE();
    case 103:
      if (lookahead == 'o') ADVANCE(145);
      END_STATE();
    case 104:
      if (lookahead == 'o') ADVANCE(116);
      END_STATE();
    case 105:
      if (lookahead == 'o') ADVANCE(95);
      END_STATE();
    case 106:
      if (lookahead == 'o') ADVANCE(101);
      END_STATE();
    case 107:
      if (lookahead == 'p') ADVANCE(12);
      END_STATE();
    case 108:
      if (lookahead == 'p') ADVANCE(110);
      END_STATE();
    case 109:
      if (lookahead == 'p') ADVANCE(135);
      END_STATE();
    case 110:
      if (lookahead == 'p') ADVANCE(56);
      END_STATE();
    case 111:
      if (lookahead == 'p') ADVANCE(144);
      END_STATE();
    case 112:
      if (lookahead == 'r') ADVANCE(191);
      END_STATE();
    case 113:
      if (lookahead == 'r') ADVANCE(196);
      END_STATE();
    case 114:
      if (lookahead == 'r') ADVANCE(142);
      END_STATE();
    case 115:
      if (lookahead == 'r') ADVANCE(31);
      END_STATE();
    case 116:
      if (lookahead == 'r') ADVANCE(79);
      END_STATE();
    case 117:
      if (lookahead == 'r') ADVANCE(7);
      END_STATE();
    case 118:
      if (lookahead == 'r') ADVANCE(100);
      END_STATE();
    case 119:
      if (lookahead == 'r') ADVANCE(18);
      END_STATE();
    case 120:
      if (lookahead == 'r') ADVANCE(54);
      END_STATE();
    case 121:
      if (lookahead == 'r') ADVANCE(23);
      END_STATE();
    case 122:
      if (lookahead == 'r') ADVANCE(71);
      END_STATE();
    case 123:
      if (lookahead == 's') ADVANCE(179);
      END_STATE();
    case 124:
      if (lookahead == 's') ADVANCE(184);
      END_STATE();
    case 125:
      if (lookahead == 's') ADVANCE(188);
      END_STATE();
    case 126:
      if (lookahead == 's') ADVANCE(189);
      END_STATE();
    case 127:
      if (lookahead == 's') ADVANCE(39);
      END_STATE();
    case 128:
      if (lookahead == 's') ADVANCE(131);
      if (lookahead == 't') ADVANCE(16);
      END_STATE();
    case 129:
      if (lookahead == 's') ADVANCE(103);
      END_STATE();
    case 130:
      if (lookahead == 's') ADVANCE(43);
      END_STATE();
    case 131:
      if (lookahead == 's') ADVANCE(20);
      END_STATE();
    case 132:
      if (lookahead == 't') ADVANCE(111);
      END_STATE();
    case 133:
      if (lookahead == 't') ADVANCE(181);
      END_STATE();
    case 134:
      if (lookahead == 't') ADVANCE(183);
      END_STATE();
    case 135:
      if (lookahead == 't') ADVANCE(193);
      END_STATE();
    case 136:
      if (lookahead == 't') ADVANCE(64);
      END_STATE();
    case 137:
      if (lookahead == 't') ADVANCE(41);
      END_STATE();
    case 138:
      if (lookahead == 't') ADVANCE(74);
      END_STATE();
    case 139:
      if (lookahead == 'u') ADVANCE(84);
      END_STATE();
    case 140:
      if (lookahead == 'u') ADVANCE(132);
      END_STATE();
    case 141:
      if (lookahead == 'u') ADVANCE(37);
      END_STATE();
    case 142:
      if (lookahead == 'u') ADVANCE(40);
      END_STATE();
    case 143:
      if (lookahead == 'u') ADVANCE(133);
      END_STATE();
    case 144:
      if (lookahead == 'u') ADVANCE(134);
      END_STATE();
    case 145:
      if (lookahead == 'u') ADVANCE(115);
      END_STATE();
    case 146:
      if (lookahead == 'u') ADVANCE(86);
      END_STATE();
    case 147:
      if (lookahead == 'v') ADVANCE(24);
      END_STATE();
    case 148:
      if (lookahead == 'w') ADVANCE(121);
      END_STATE();
    case 149:
      if (lookahead == '{') ADVANCE(201);
      END_STATE();
    case 150:
      if (lookahead == '}') ADVANCE(202);
      END_STATE();
    case 151:
      if (lookahead == '0' ||
          lookahead == '1') ADVANCE(210);
      END_STATE();
    case 152:
      if (('0' <= lookahead && lookahead <= '7')) ADVANCE(211);
      END_STATE();
    case 153:
      if (('0' <= lookahead && lookahead <= '9')) ADVANCE(203);
      END_STATE();
    case 154:
      if (('0' <= lookahead && lookahead <= '9')) ADVANCE(153);
      END_STATE();
    case 155:
      if (('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'F') ||
          ('a' <= lookahead && lookahead <= 'f')) ADVANCE(212);
      END_STATE();
    case 156:
      if (('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'F') ||
          ('a' <= lookahead && lookahead <= 'f')) ADVANCE(203);
      END_STATE();
    case 157:
      if (('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'F') ||
          ('a' <= lookahead && lookahead <= 'f')) ADVANCE(156);
      END_STATE();
    case 158:
      if (('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'F') ||
          ('a' <= lookahead && lookahead <= 'f')) ADVANCE(157);
      END_STATE();
    case 159:
      if (('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'F') ||
          ('a' <= lookahead && lookahead <= 'f')) ADVANCE(158);
      END_STATE();
    case 160:
      if (('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'F') ||
          ('a' <= lookahead && lookahead <= 'f')) ADVANCE(159);
      END_STATE();
    case 161:
      if (('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'F') ||
          ('a' <= lookahead && lookahead <= 'f')) ADVANCE(160);
      END_STATE();
    case 162:
      if (('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'F') ||
          ('a' <= lookahead && lookahead <= 'f')) ADVANCE(161);
      END_STATE();
    case 163:
      if (('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'F') ||
          ('a' <= lookahead && lookahead <= 'f')) ADVANCE(162);
      END_STATE();
    case 164:
      if (eof) ADVANCE(169);
      if (lookahead == '\n') SKIP(0)
      END_STATE();
    case 165:
      if (eof) ADVANCE(169);
      if (lookahead == '\n') SKIP(0)
      if (lookahead == '\r') SKIP(164)
      END_STATE();
    case 166:
      if (eof) ADVANCE(169);
      if (lookahead == '\n') SKIP(168)
      END_STATE();
    case 167:
      if (eof) ADVANCE(169);
      if (lookahead == '\n') SKIP(168)
      if (lookahead == '\r') SKIP(166)
      END_STATE();
    case 168:
      if (eof) ADVANCE(169);
      if (lookahead == '#') ADVANCE(199);
      if (lookahead == '\\') SKIP(167)
      if (lookahead == 'c') ADVANCE(105);
      if (lookahead == 'i') ADVANCE(96);
      if (lookahead == 'r') ADVANCE(139);
      if (lookahead == 'w') ADVANCE(104);
      if (lookahead == '\t' ||
          lookahead == '\n' ||
          lookahead == '\f' ||
          lookahead == '\r' ||
          lookahead == ' ' ||
          lookahead == 8203 ||
          lookahead == 8288 ||
          lookahead == 65279) SKIP(168)
      END_STATE();
    case 169:
      ACCEPT_TOKEN(ts_builtin_sym_end);
      END_STATE();
    case 170:
      ACCEPT_TOKEN(anon_sym_rule);
      END_STATE();
    case 171:
      ACCEPT_TOKEN(anon_sym_COLON);
      END_STATE();
    case 172:
      ACCEPT_TOKEN(anon_sym_include_COLON);
      END_STATE();
    case 173:
      ACCEPT_TOKEN(anon_sym_workdir_COLON);
      END_STATE();
    case 174:
      ACCEPT_TOKEN(anon_sym_module);
      END_STATE();
    case 175:
      ACCEPT_TOKEN(anon_sym_configfile);
      END_STATE();
    case 176:
      ACCEPT_TOKEN(anon_sym_use);
      END_STATE();
    case 177:
      ACCEPT_TOKEN(anon_sym_STAR);
      END_STATE();
    case 178:
      ACCEPT_TOKEN(anon_sym_from);
      END_STATE();
    case 179:
      ACCEPT_TOKEN(anon_sym_as);
      END_STATE();
    case 180:
      ACCEPT_TOKEN(anon_sym_with);
      END_STATE();
    case 181:
      ACCEPT_TOKEN(anon_sym_input);
      END_STATE();
    case 182:
      ACCEPT_TOKEN(anon_sym_COMMA);
      END_STATE();
    case 183:
      ACCEPT_TOKEN(anon_sym_output);
      END_STATE();
    case 184:
      ACCEPT_TOKEN(anon_sym_params);
      END_STATE();
    case 185:
      ACCEPT_TOKEN(anon_sym_log);
      END_STATE();
    case 186:
      ACCEPT_TOKEN(anon_sym_cache);
      END_STATE();
    case 187:
      ACCEPT_TOKEN(anon_sym_message);
      END_STATE();
    case 188:
      ACCEPT_TOKEN(anon_sym_threads);
      END_STATE();
    case 189:
      ACCEPT_TOKEN(anon_sym_resources);
      END_STATE();
    case 190:
      ACCEPT_TOKEN(anon_sym_conda);
      END_STATE();
    case 191:
      ACCEPT_TOKEN(anon_sym_container);
      END_STATE();
    case 192:
      ACCEPT_TOKEN(anon_sym_shell);
      END_STATE();
    case 193:
      ACCEPT_TOKEN(anon_sym_script);
      END_STATE();
    case 194:
      ACCEPT_TOKEN(anon_sym_notebook);
      END_STATE();
    case 195:
      ACCEPT_TOKEN(anon_sym_snakefile);
      END_STATE();
    case 196:
      ACCEPT_TOKEN(anon_sym_meta_wrapper);
      END_STATE();
    case 197:
      ACCEPT_TOKEN(anon_sym_config);
      if (lookahead == 'f') ADVANCE(77);
      END_STATE();
    case 198:
      ACCEPT_TOKEN(anon_sym_skip_validation);
      END_STATE();
    case 199:
      ACCEPT_TOKEN(sym_comment);
      if (lookahead != 0 &&
          lookahead != '\n') ADVANCE(199);
      END_STATE();
    case 200:
      ACCEPT_TOKEN(sym_identifier);
      if (sym_identifier_character_set_2(lookahead)) ADVANCE(200);
      END_STATE();
    case 201:
      ACCEPT_TOKEN(anon_sym_LBRACE_LBRACE);
      END_STATE();
    case 202:
      ACCEPT_TOKEN(anon_sym_RBRACE_RBRACE);
      END_STATE();
    case 203:
      ACCEPT_TOKEN(sym_escape_sequence);
      END_STATE();
    case 204:
      ACCEPT_TOKEN(sym_escape_sequence);
      if (lookahead == '\\') ADVANCE(205);
      END_STATE();
    case 205:
      ACCEPT_TOKEN(sym__not_escape_sequence);
      if (lookahead == '\n') ADVANCE(204);
      if (lookahead == '\r') ADVANCE(1);
      if (lookahead == 'U') ADVANCE(163);
      if (lookahead == 'u') ADVANCE(159);
      if (lookahead == 'x') ADVANCE(157);
      if (lookahead == '"' ||
          lookahead == '\'' ||
          lookahead == '\\' ||
          lookahead == 'a' ||
          lookahead == 'b' ||
          lookahead == 'f' ||
          lookahead == 'n' ||
          lookahead == 'r' ||
          ('t' <= lookahead && lookahead <= 'v')) ADVANCE(203);
      if (('0' <= lookahead && lookahead <= '9')) ADVANCE(154);
      END_STATE();
    case 206:
      ACCEPT_TOKEN(anon_sym_True);
      END_STATE();
    case 207:
      ACCEPT_TOKEN(anon_sym_False);
      END_STATE();
    case 208:
      ACCEPT_TOKEN(sym_integer);
      END_STATE();
    case 209:
      ACCEPT_TOKEN(sym_integer);
      if (lookahead == '_') ADVANCE(214);
      if (lookahead == 'J' ||
          lookahead == 'L' ||
          lookahead == 'j' ||
          lookahead == 'l') ADVANCE(208);
      if (('0' <= lookahead && lookahead <= '9')) ADVANCE(209);
      END_STATE();
    case 210:
      ACCEPT_TOKEN(sym_integer);
      if (lookahead == '_') ADVANCE(151);
      if (lookahead == 'L' ||
          lookahead == 'l') ADVANCE(208);
      if (lookahead == '0' ||
          lookahead == '1') ADVANCE(210);
      END_STATE();
    case 211:
      ACCEPT_TOKEN(sym_integer);
      if (lookahead == '_') ADVANCE(152);
      if (lookahead == 'L' ||
          lookahead == 'l') ADVANCE(208);
      if (('0' <= lookahead && lookahead <= '7')) ADVANCE(211);
      END_STATE();
    case 212:
      ACCEPT_TOKEN(sym_integer);
      if (lookahead == '_') ADVANCE(155);
      if (lookahead == 'L' ||
          lookahead == 'l') ADVANCE(208);
      if (('0' <= lookahead && lookahead <= '9') ||
          ('A' <= lookahead && lookahead <= 'F') ||
          ('a' <= lookahead && lookahead <= 'f')) ADVANCE(212);
      END_STATE();
    case 213:
      ACCEPT_TOKEN(sym_integer);
      if (lookahead == 'B' ||
          lookahead == 'b') ADVANCE(8);
      if (lookahead == 'O' ||
          lookahead == 'o') ADVANCE(9);
      if (lookahead == 'X' ||
          lookahead == 'x') ADVANCE(10);
      if (lookahead == '_') ADVANCE(214);
      if (lookahead == 'J' ||
          lookahead == 'L' ||
          lookahead == 'j' ||
          lookahead == 'l') ADVANCE(208);
      if (('0' <= lookahead && lookahead <= '9')) ADVANCE(209);
      END_STATE();
    case 214:
      ACCEPT_TOKEN(sym_integer);
      if (lookahead == 'J' ||
          lookahead == 'L' ||
          lookahead == 'j' ||
          lookahead == 'l') ADVANCE(208);
      if (('0' <= lookahead && lookahead <= '9')) ADVANCE(209);
      END_STATE();
    default:
      return false;
  }
}

static const TSLexMode ts_lex_modes[STATE_COUNT] = {
  [0] = {.lex_state = 0, .external_lex_state = 1},
  [1] = {.lex_state = 168},
  [2] = {.lex_state = 0},
  [3] = {.lex_state = 0},
  [4] = {.lex_state = 168},
  [5] = {.lex_state = 168},
  [6] = {.lex_state = 4, .external_lex_state = 2},
  [7] = {.lex_state = 4, .external_lex_state = 2},
  [8] = {.lex_state = 4, .external_lex_state = 2},
  [9] = {.lex_state = 4, .external_lex_state = 2},
  [10] = {.lex_state = 4, .external_lex_state = 2},
  [11] = {.lex_state = 4, .external_lex_state = 2},
  [12] = {.lex_state = 4, .external_lex_state = 2},
  [13] = {.lex_state = 168, .external_lex_state = 3},
  [14] = {.lex_state = 168},
  [15] = {.lex_state = 4, .external_lex_state = 2},
  [16] = {.lex_state = 168},
  [17] = {.lex_state = 168},
  [18] = {.lex_state = 168},
  [19] = {.lex_state = 168},
  [20] = {.lex_state = 168},
  [21] = {.lex_state = 168},
  [22] = {.lex_state = 0, .external_lex_state = 4},
  [23] = {.lex_state = 0, .external_lex_state = 5},
  [24] = {.lex_state = 0, .external_lex_state = 4},
  [25] = {.lex_state = 0, .external_lex_state = 5},
  [26] = {.lex_state = 0, .external_lex_state = 6},
  [27] = {.lex_state = 0, .external_lex_state = 5},
  [28] = {.lex_state = 0, .external_lex_state = 6},
  [29] = {.lex_state = 0, .external_lex_state = 5},
  [30] = {.lex_state = 0, .external_lex_state = 4},
  [31] = {.lex_state = 0, .external_lex_state = 5},
  [32] = {.lex_state = 0, .external_lex_state = 5},
  [33] = {.lex_state = 0, .external_lex_state = 4},
  [34] = {.lex_state = 0, .external_lex_state = 6},
  [35] = {.lex_state = 0, .external_lex_state = 5},
  [36] = {.lex_state = 0, .external_lex_state = 5},
  [37] = {.lex_state = 0, .external_lex_state = 5},
  [38] = {.lex_state = 0, .external_lex_state = 4},
  [39] = {.lex_state = 0, .external_lex_state = 4},
  [40] = {.lex_state = 0},
  [41] = {.lex_state = 0, .external_lex_state = 4},
  [42] = {.lex_state = 0, .external_lex_state = 4},
  [43] = {.lex_state = 0, .external_lex_state = 4},
  [44] = {.lex_state = 0, .external_lex_state = 5},
  [45] = {.lex_state = 0, .external_lex_state = 4},
  [46] = {.lex_state = 0, .external_lex_state = 5},
  [47] = {.lex_state = 0, .external_lex_state = 4},
  [48] = {.lex_state = 0, .external_lex_state = 4},
  [49] = {.lex_state = 0, .external_lex_state = 4},
  [50] = {.lex_state = 0, .external_lex_state = 4},
  [51] = {.lex_state = 0, .external_lex_state = 6},
  [52] = {.lex_state = 0, .external_lex_state = 6},
  [53] = {.lex_state = 0, .external_lex_state = 5},
  [54] = {.lex_state = 0, .external_lex_state = 5},
  [55] = {.lex_state = 0, .external_lex_state = 4},
  [56] = {.lex_state = 0, .external_lex_state = 4},
  [57] = {.lex_state = 0, .external_lex_state = 4},
  [58] = {.lex_state = 0, .external_lex_state = 6},
  [59] = {.lex_state = 0},
  [60] = {.lex_state = 0, .external_lex_state = 3},
  [61] = {.lex_state = 0, .external_lex_state = 5},
  [62] = {.lex_state = 0, .external_lex_state = 5},
  [63] = {.lex_state = 0, .external_lex_state = 5},
  [64] = {.lex_state = 0, .external_lex_state = 5},
  [65] = {.lex_state = 0, .external_lex_state = 5},
  [66] = {.lex_state = 0},
  [67] = {.lex_state = 0},
  [68] = {.lex_state = 0},
  [69] = {.lex_state = 0, .external_lex_state = 5},
  [70] = {.lex_state = 0, .external_lex_state = 5},
  [71] = {.lex_state = 0},
  [72] = {.lex_state = 0},
  [73] = {.lex_state = 0},
  [74] = {.lex_state = 0},
  [75] = {.lex_state = 0},
  [76] = {.lex_state = 0, .external_lex_state = 5},
  [77] = {.lex_state = 0, .external_lex_state = 5},
  [78] = {.lex_state = 0, .external_lex_state = 5},
  [79] = {.lex_state = 0, .external_lex_state = 5},
  [80] = {.lex_state = 0},
  [81] = {.lex_state = 5},
  [82] = {.lex_state = 0, .external_lex_state = 5},
  [83] = {.lex_state = 0},
  [84] = {.lex_state = 0},
  [85] = {.lex_state = 0},
  [86] = {.lex_state = 0, .external_lex_state = 5},
  [87] = {.lex_state = 0},
  [88] = {.lex_state = 0},
  [89] = {.lex_state = 0},
  [90] = {.lex_state = 0},
  [91] = {.lex_state = 0},
};

enum {
  ts_external_token__newline = 0,
  ts_external_token__indent = 1,
  ts_external_token__dedent = 2,
  ts_external_token__string_start = 3,
  ts_external_token__string_content = 4,
  ts_external_token__string_end = 5,
};

static const TSSymbol ts_external_scanner_symbol_map[EXTERNAL_TOKEN_COUNT] = {
  [ts_external_token__newline] = sym__newline,
  [ts_external_token__indent] = sym__indent,
  [ts_external_token__dedent] = sym__dedent,
  [ts_external_token__string_start] = sym__string_start,
  [ts_external_token__string_content] = sym__string_content,
  [ts_external_token__string_end] = sym__string_end,
};

static const bool ts_external_scanner_states[7][EXTERNAL_TOKEN_COUNT] = {
  [1] = {
    [ts_external_token__newline] = true,
    [ts_external_token__indent] = true,
    [ts_external_token__dedent] = true,
    [ts_external_token__string_start] = true,
    [ts_external_token__string_content] = true,
    [ts_external_token__string_end] = true,
  },
  [2] = {
    [ts_external_token__string_content] = true,
    [ts_external_token__string_end] = true,
  },
  [3] = {
    [ts_external_token__indent] = true,
  },
  [4] = {
    [ts_external_token__string_start] = true,
  },
  [5] = {
    [ts_external_token__newline] = true,
  },
  [6] = {
    [ts_external_token__newline] = true,
    [ts_external_token__indent] = true,
  },
};

static const uint16_t ts_parse_table[LARGE_STATE_COUNT][SYMBOL_COUNT] = {
  [0] = {
    [ts_builtin_sym_end] = ACTIONS(1),
    [anon_sym_rule] = ACTIONS(1),
    [anon_sym_COLON] = ACTIONS(1),
    [anon_sym_include_COLON] = ACTIONS(1),
    [anon_sym_workdir_COLON] = ACTIONS(1),
    [anon_sym_module] = ACTIONS(1),
    [anon_sym_configfile] = ACTIONS(1),
    [anon_sym_use] = ACTIONS(1),
    [anon_sym_STAR] = ACTIONS(1),
    [anon_sym_from] = ACTIONS(1),
    [anon_sym_as] = ACTIONS(1),
    [anon_sym_with] = ACTIONS(1),
    [anon_sym_input] = ACTIONS(1),
    [anon_sym_COMMA] = ACTIONS(1),
    [anon_sym_output] = ACTIONS(1),
    [anon_sym_params] = ACTIONS(1),
    [anon_sym_log] = ACTIONS(1),
    [anon_sym_cache] = ACTIONS(1),
    [anon_sym_message] = ACTIONS(1),
    [anon_sym_threads] = ACTIONS(1),
    [anon_sym_resources] = ACTIONS(1),
    [anon_sym_conda] = ACTIONS(1),
    [anon_sym_container] = ACTIONS(1),
    [anon_sym_shell] = ACTIONS(1),
    [anon_sym_script] = ACTIONS(1),
    [anon_sym_notebook] = ACTIONS(1),
    [anon_sym_snakefile] = ACTIONS(1),
    [anon_sym_meta_wrapper] = ACTIONS(1),
    [anon_sym_config] = ACTIONS(1),
    [anon_sym_skip_validation] = ACTIONS(1),
    [sym_comment] = ACTIONS(3),
    [anon_sym_LBRACE_LBRACE] = ACTIONS(1),
    [anon_sym_RBRACE_RBRACE] = ACTIONS(1),
    [anon_sym_True] = ACTIONS(1),
    [anon_sym_False] = ACTIONS(1),
    [sym_integer] = ACTIONS(1),
    [sym__newline] = ACTIONS(1),
    [sym__indent] = ACTIONS(1),
    [sym__dedent] = ACTIONS(1),
    [sym__string_start] = ACTIONS(1),
    [sym__string_content] = ACTIONS(1),
    [sym__string_end] = ACTIONS(1),
  },
  [1] = {
    [sym_snakemake] = STATE(83),
    [sym_rule] = STATE(4),
    [sym__include] = STATE(4),
    [sym__workdir] = STATE(4),
    [sym__configfile] = STATE(4),
    [sym_container] = STATE(4),
    [aux_sym_snakemake_repeat1] = STATE(4),
    [ts_builtin_sym_end] = ACTIONS(5),
    [anon_sym_rule] = ACTIONS(7),
    [anon_sym_include_COLON] = ACTIONS(9),
    [anon_sym_workdir_COLON] = ACTIONS(11),
    [anon_sym_configfile] = ACTIONS(13),
    [anon_sym_container] = ACTIONS(15),
    [sym_comment] = ACTIONS(3),
  },
};

static const uint16_t ts_small_parse_table[] = {
  [0] = 16,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(17), 1,
      anon_sym_input,
    ACTIONS(19), 1,
      anon_sym_output,
    ACTIONS(21), 1,
      anon_sym_params,
    ACTIONS(23), 1,
      anon_sym_log,
    ACTIONS(25), 1,
      anon_sym_cache,
    ACTIONS(27), 1,
      anon_sym_message,
    ACTIONS(29), 1,
      anon_sym_threads,
    ACTIONS(31), 1,
      anon_sym_resources,
    ACTIONS(33), 1,
      anon_sym_conda,
    ACTIONS(35), 1,
      anon_sym_container,
    ACTIONS(37), 1,
      anon_sym_shell,
    ACTIONS(39), 1,
      anon_sym_script,
    ACTIONS(41), 1,
      anon_sym_notebook,
    STATE(78), 1,
      sym__params,
    STATE(77), 12,
      sym_input,
      sym_output,
      sym_log,
      sym_cache,
      sym_message,
      sym_threads,
      sym__resources,
      sym_conda,
      sym_container,
      sym_shell,
      sym_script,
      sym_notebook,
  [60] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(43), 13,
      anon_sym_input,
      anon_sym_output,
      anon_sym_params,
      anon_sym_log,
      anon_sym_cache,
      anon_sym_message,
      anon_sym_threads,
      anon_sym_resources,
      anon_sym_conda,
      anon_sym_container,
      anon_sym_shell,
      anon_sym_script,
      anon_sym_notebook,
  [79] = 8,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(7), 1,
      anon_sym_rule,
    ACTIONS(9), 1,
      anon_sym_include_COLON,
    ACTIONS(11), 1,
      anon_sym_workdir_COLON,
    ACTIONS(13), 1,
      anon_sym_configfile,
    ACTIONS(15), 1,
      anon_sym_container,
    ACTIONS(45), 1,
      ts_builtin_sym_end,
    STATE(5), 6,
      sym_rule,
      sym__include,
      sym__workdir,
      sym__configfile,
      sym_container,
      aux_sym_snakemake_repeat1,
  [109] = 8,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(47), 1,
      ts_builtin_sym_end,
    ACTIONS(49), 1,
      anon_sym_rule,
    ACTIONS(52), 1,
      anon_sym_include_COLON,
    ACTIONS(55), 1,
      anon_sym_workdir_COLON,
    ACTIONS(58), 1,
      anon_sym_configfile,
    ACTIONS(61), 1,
      anon_sym_container,
    STATE(5), 6,
      sym_rule,
      sym__include,
      sym__workdir,
      sym__configfile,
      sym_container,
      aux_sym_snakemake_repeat1,
  [139] = 6,
    ACTIONS(64), 1,
      sym_comment,
    ACTIONS(69), 1,
      sym__string_content,
    ACTIONS(72), 1,
      sym__string_end,
    STATE(6), 1,
      aux_sym_string_repeat1,
    STATE(15), 1,
      sym__escape_interpolation,
    ACTIONS(66), 4,
      anon_sym_LBRACE_LBRACE,
      anon_sym_RBRACE_RBRACE,
      sym_escape_sequence,
      sym__not_escape_sequence,
  [161] = 6,
    ACTIONS(64), 1,
      sym_comment,
    ACTIONS(76), 1,
      sym__string_content,
    ACTIONS(78), 1,
      sym__string_end,
    STATE(6), 1,
      aux_sym_string_repeat1,
    STATE(15), 1,
      sym__escape_interpolation,
    ACTIONS(74), 4,
      anon_sym_LBRACE_LBRACE,
      anon_sym_RBRACE_RBRACE,
      sym_escape_sequence,
      sym__not_escape_sequence,
  [183] = 6,
    ACTIONS(64), 1,
      sym_comment,
    ACTIONS(76), 1,
      sym__string_content,
    ACTIONS(80), 1,
      sym__string_end,
    STATE(7), 1,
      aux_sym_string_repeat1,
    STATE(15), 1,
      sym__escape_interpolation,
    ACTIONS(74), 4,
      anon_sym_LBRACE_LBRACE,
      anon_sym_RBRACE_RBRACE,
      sym_escape_sequence,
      sym__not_escape_sequence,
  [205] = 6,
    ACTIONS(64), 1,
      sym_comment,
    ACTIONS(76), 1,
      sym__string_content,
    ACTIONS(82), 1,
      sym__string_end,
    STATE(6), 1,
      aux_sym_string_repeat1,
    STATE(15), 1,
      sym__escape_interpolation,
    ACTIONS(74), 4,
      anon_sym_LBRACE_LBRACE,
      anon_sym_RBRACE_RBRACE,
      sym_escape_sequence,
      sym__not_escape_sequence,
  [227] = 6,
    ACTIONS(64), 1,
      sym_comment,
    ACTIONS(76), 1,
      sym__string_content,
    ACTIONS(84), 1,
      sym__string_end,
    STATE(9), 1,
      aux_sym_string_repeat1,
    STATE(15), 1,
      sym__escape_interpolation,
    ACTIONS(74), 4,
      anon_sym_LBRACE_LBRACE,
      anon_sym_RBRACE_RBRACE,
      sym_escape_sequence,
      sym__not_escape_sequence,
  [249] = 6,
    ACTIONS(64), 1,
      sym_comment,
    ACTIONS(76), 1,
      sym__string_content,
    ACTIONS(86), 1,
      sym__string_end,
    STATE(12), 1,
      aux_sym_string_repeat1,
    STATE(15), 1,
      sym__escape_interpolation,
    ACTIONS(74), 4,
      anon_sym_LBRACE_LBRACE,
      anon_sym_RBRACE_RBRACE,
      sym_escape_sequence,
      sym__not_escape_sequence,
  [271] = 6,
    ACTIONS(64), 1,
      sym_comment,
    ACTIONS(76), 1,
      sym__string_content,
    ACTIONS(88), 1,
      sym__string_end,
    STATE(6), 1,
      aux_sym_string_repeat1,
    STATE(15), 1,
      sym__escape_interpolation,
    ACTIONS(74), 4,
      anon_sym_LBRACE_LBRACE,
      anon_sym_RBRACE_RBRACE,
      sym_escape_sequence,
      sym__not_escape_sequence,
  [293] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(92), 1,
      sym__indent,
    ACTIONS(90), 6,
      ts_builtin_sym_end,
      anon_sym_rule,
      anon_sym_include_COLON,
      anon_sym_workdir_COLON,
      anon_sym_configfile,
      anon_sym_container,
  [308] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(94), 6,
      ts_builtin_sym_end,
      anon_sym_rule,
      anon_sym_include_COLON,
      anon_sym_workdir_COLON,
      anon_sym_configfile,
      anon_sym_container,
  [320] = 3,
    ACTIONS(64), 1,
      sym_comment,
    ACTIONS(98), 2,
      sym__string_content,
      sym__string_end,
    ACTIONS(96), 4,
      anon_sym_LBRACE_LBRACE,
      anon_sym_RBRACE_RBRACE,
      sym_escape_sequence,
      sym__not_escape_sequence,
  [334] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(100), 6,
      ts_builtin_sym_end,
      anon_sym_rule,
      anon_sym_include_COLON,
      anon_sym_workdir_COLON,
      anon_sym_configfile,
      anon_sym_container,
  [346] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(102), 6,
      ts_builtin_sym_end,
      anon_sym_rule,
      anon_sym_include_COLON,
      anon_sym_workdir_COLON,
      anon_sym_configfile,
      anon_sym_container,
  [358] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(104), 6,
      ts_builtin_sym_end,
      anon_sym_rule,
      anon_sym_include_COLON,
      anon_sym_workdir_COLON,
      anon_sym_configfile,
      anon_sym_container,
  [370] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(106), 6,
      ts_builtin_sym_end,
      anon_sym_rule,
      anon_sym_include_COLON,
      anon_sym_workdir_COLON,
      anon_sym_configfile,
      anon_sym_container,
  [382] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(108), 6,
      ts_builtin_sym_end,
      anon_sym_rule,
      anon_sym_include_COLON,
      anon_sym_workdir_COLON,
      anon_sym_configfile,
      anon_sym_container,
  [394] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(110), 6,
      ts_builtin_sym_end,
      anon_sym_rule,
      anon_sym_include_COLON,
      anon_sym_workdir_COLON,
      anon_sym_configfile,
      anon_sym_container,
  [406] = 4,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(112), 1,
      sym__string_start,
    STATE(86), 1,
      sym__shellparams,
    STATE(34), 2,
      sym__parameter_list,
      sym_string,
  [420] = 5,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(114), 1,
      sym__newline,
    STATE(2), 1,
      sym__ni,
    STATE(21), 1,
      sym__ruleparams,
    STATE(29), 1,
      aux_sym__norunparams,
  [436] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(116), 1,
      sym__string_start,
    STATE(36), 2,
      sym__parameter_list,
      sym_string,
  [447] = 4,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(118), 1,
      anon_sym_COMMA,
    ACTIONS(120), 1,
      sym__newline,
    STATE(27), 1,
      aux_sym_input_repeat1,
  [460] = 4,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(122), 1,
      sym__newline,
    ACTIONS(124), 1,
      sym__indent,
    STATE(26), 1,
      aux_sym__shellparams_repeat1,
  [473] = 4,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(127), 1,
      anon_sym_COMMA,
    ACTIONS(130), 1,
      sym__newline,
    STATE(27), 1,
      aux_sym_input_repeat1,
  [486] = 4,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(132), 1,
      sym__newline,
    ACTIONS(134), 1,
      sym__indent,
    STATE(26), 1,
      aux_sym__shellparams_repeat1,
  [499] = 4,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(136), 1,
      sym__newline,
    STATE(2), 1,
      sym__ni,
    STATE(44), 1,
      aux_sym__norunparams,
  [512] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(112), 1,
      sym__string_start,
    STATE(58), 2,
      sym__parameter_list,
      sym_string,
  [523] = 4,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(118), 1,
      anon_sym_COMMA,
    ACTIONS(138), 1,
      sym__newline,
    STATE(27), 1,
      aux_sym_input_repeat1,
  [536] = 4,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(118), 1,
      anon_sym_COMMA,
    ACTIONS(140), 1,
      sym__newline,
    STATE(27), 1,
      aux_sym_input_repeat1,
  [549] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(116), 1,
      sym__string_start,
    STATE(46), 2,
      sym__parameter_list,
      sym_string,
  [560] = 4,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(134), 1,
      sym__indent,
    ACTIONS(142), 1,
      sym__newline,
    STATE(28), 1,
      aux_sym__shellparams_repeat1,
  [573] = 4,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(118), 1,
      anon_sym_COMMA,
    ACTIONS(144), 1,
      sym__newline,
    STATE(31), 1,
      aux_sym_input_repeat1,
  [586] = 4,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(118), 1,
      anon_sym_COMMA,
    ACTIONS(146), 1,
      sym__newline,
    STATE(25), 1,
      aux_sym_input_repeat1,
  [599] = 4,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(118), 1,
      anon_sym_COMMA,
    ACTIONS(148), 1,
      sym__newline,
    STATE(32), 1,
      aux_sym_input_repeat1,
  [612] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(116), 1,
      sym__string_start,
    STATE(35), 2,
      sym__parameter_list,
      sym_string,
  [623] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(116), 1,
      sym__string_start,
    STATE(65), 2,
      sym__parameter_list,
      sym_string,
  [634] = 3,
    ACTIONS(3), 1,
      sym_comment,
    STATE(62), 1,
      sym_boolean,
    ACTIONS(150), 2,
      anon_sym_True,
      anon_sym_False,
  [645] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(116), 1,
      sym__string_start,
    STATE(76), 2,
      sym__parameter_list,
      sym_string,
  [656] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(116), 1,
      sym__string_start,
    STATE(79), 2,
      sym__parameter_list,
      sym_string,
  [667] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(116), 1,
      sym__string_start,
    STATE(37), 2,
      sym__parameter_list,
      sym_string,
  [678] = 4,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(152), 1,
      sym__newline,
    STATE(2), 1,
      sym__ni,
    STATE(44), 1,
      aux_sym__norunparams,
  [691] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(155), 1,
      sym__string_start,
    STATE(19), 1,
      sym_string,
  [701] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(130), 2,
      sym__newline,
      anon_sym_COMMA,
  [709] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(155), 1,
      sym__string_start,
    STATE(20), 1,
      sym_string,
  [719] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(155), 1,
      sym__string_start,
    STATE(17), 1,
      sym_string,
  [729] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(116), 1,
      sym__string_start,
    STATE(82), 1,
      sym_string,
  [739] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(116), 1,
      sym__string_start,
    STATE(69), 1,
      sym_string,
  [749] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(100), 2,
      sym__newline,
      sym__indent,
  [757] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(94), 2,
      sym__newline,
      sym__indent,
  [765] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(100), 2,
      sym__newline,
      anon_sym_COMMA,
  [773] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(94), 2,
      sym__newline,
      anon_sym_COMMA,
  [781] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(116), 1,
      sym__string_start,
    STATE(63), 1,
      sym_string,
  [791] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(116), 1,
      sym__string_start,
    STATE(70), 1,
      sym_string,
  [801] = 3,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(155), 1,
      sym__string_start,
    STATE(18), 1,
      sym_string,
  [811] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(122), 2,
      sym__newline,
      sym__indent,
  [819] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(157), 1,
      anon_sym_COLON,
  [826] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(92), 1,
      sym__indent,
  [833] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(159), 1,
      sym__newline,
  [840] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(161), 1,
      sym__newline,
  [847] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(163), 1,
      sym__newline,
  [854] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(165), 1,
      sym__newline,
  [861] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(167), 1,
      sym__newline,
  [868] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(169), 1,
      anon_sym_COLON,
  [875] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(171), 1,
      anon_sym_COLON,
  [882] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(173), 1,
      anon_sym_COLON,
  [889] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(175), 1,
      sym__newline,
  [896] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(177), 1,
      sym__newline,
  [903] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(179), 1,
      anon_sym_COLON,
  [910] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(181), 1,
      anon_sym_COLON,
  [917] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(183), 1,
      anon_sym_COLON,
  [924] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(185), 1,
      anon_sym_COLON,
  [931] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(187), 1,
      anon_sym_COLON,
  [938] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(189), 1,
      sym__newline,
  [945] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(191), 1,
      sym__newline,
  [952] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(193), 1,
      sym__newline,
  [959] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(195), 1,
      sym__newline,
  [966] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(197), 1,
      anon_sym_COLON,
  [973] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(199), 1,
      sym_identifier,
  [980] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(106), 1,
      sym__newline,
  [987] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(201), 1,
      ts_builtin_sym_end,
  [994] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(203), 1,
      anon_sym_COLON,
  [1001] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(205), 1,
      anon_sym_COLON,
  [1008] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(207), 1,
      sym__newline,
  [1015] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(209), 1,
      sym_integer,
  [1022] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(211), 1,
      anon_sym_COLON,
  [1029] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(213), 1,
      anon_sym_COLON,
  [1036] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(215), 1,
      anon_sym_COLON,
  [1043] = 2,
    ACTIONS(3), 1,
      sym_comment,
    ACTIONS(217), 1,
      anon_sym_COLON,
};

static const uint32_t ts_small_parse_table_map[] = {
  [SMALL_STATE(2)] = 0,
  [SMALL_STATE(3)] = 60,
  [SMALL_STATE(4)] = 79,
  [SMALL_STATE(5)] = 109,
  [SMALL_STATE(6)] = 139,
  [SMALL_STATE(7)] = 161,
  [SMALL_STATE(8)] = 183,
  [SMALL_STATE(9)] = 205,
  [SMALL_STATE(10)] = 227,
  [SMALL_STATE(11)] = 249,
  [SMALL_STATE(12)] = 271,
  [SMALL_STATE(13)] = 293,
  [SMALL_STATE(14)] = 308,
  [SMALL_STATE(15)] = 320,
  [SMALL_STATE(16)] = 334,
  [SMALL_STATE(17)] = 346,
  [SMALL_STATE(18)] = 358,
  [SMALL_STATE(19)] = 370,
  [SMALL_STATE(20)] = 382,
  [SMALL_STATE(21)] = 394,
  [SMALL_STATE(22)] = 406,
  [SMALL_STATE(23)] = 420,
  [SMALL_STATE(24)] = 436,
  [SMALL_STATE(25)] = 447,
  [SMALL_STATE(26)] = 460,
  [SMALL_STATE(27)] = 473,
  [SMALL_STATE(28)] = 486,
  [SMALL_STATE(29)] = 499,
  [SMALL_STATE(30)] = 512,
  [SMALL_STATE(31)] = 523,
  [SMALL_STATE(32)] = 536,
  [SMALL_STATE(33)] = 549,
  [SMALL_STATE(34)] = 560,
  [SMALL_STATE(35)] = 573,
  [SMALL_STATE(36)] = 586,
  [SMALL_STATE(37)] = 599,
  [SMALL_STATE(38)] = 612,
  [SMALL_STATE(39)] = 623,
  [SMALL_STATE(40)] = 634,
  [SMALL_STATE(41)] = 645,
  [SMALL_STATE(42)] = 656,
  [SMALL_STATE(43)] = 667,
  [SMALL_STATE(44)] = 678,
  [SMALL_STATE(45)] = 691,
  [SMALL_STATE(46)] = 701,
  [SMALL_STATE(47)] = 709,
  [SMALL_STATE(48)] = 719,
  [SMALL_STATE(49)] = 729,
  [SMALL_STATE(50)] = 739,
  [SMALL_STATE(51)] = 749,
  [SMALL_STATE(52)] = 757,
  [SMALL_STATE(53)] = 765,
  [SMALL_STATE(54)] = 773,
  [SMALL_STATE(55)] = 781,
  [SMALL_STATE(56)] = 791,
  [SMALL_STATE(57)] = 801,
  [SMALL_STATE(58)] = 811,
  [SMALL_STATE(59)] = 819,
  [SMALL_STATE(60)] = 826,
  [SMALL_STATE(61)] = 833,
  [SMALL_STATE(62)] = 840,
  [SMALL_STATE(63)] = 847,
  [SMALL_STATE(64)] = 854,
  [SMALL_STATE(65)] = 861,
  [SMALL_STATE(66)] = 868,
  [SMALL_STATE(67)] = 875,
  [SMALL_STATE(68)] = 882,
  [SMALL_STATE(69)] = 889,
  [SMALL_STATE(70)] = 896,
  [SMALL_STATE(71)] = 903,
  [SMALL_STATE(72)] = 910,
  [SMALL_STATE(73)] = 917,
  [SMALL_STATE(74)] = 924,
  [SMALL_STATE(75)] = 931,
  [SMALL_STATE(76)] = 938,
  [SMALL_STATE(77)] = 945,
  [SMALL_STATE(78)] = 952,
  [SMALL_STATE(79)] = 959,
  [SMALL_STATE(80)] = 966,
  [SMALL_STATE(81)] = 973,
  [SMALL_STATE(82)] = 980,
  [SMALL_STATE(83)] = 987,
  [SMALL_STATE(84)] = 994,
  [SMALL_STATE(85)] = 1001,
  [SMALL_STATE(86)] = 1008,
  [SMALL_STATE(87)] = 1015,
  [SMALL_STATE(88)] = 1022,
  [SMALL_STATE(89)] = 1029,
  [SMALL_STATE(90)] = 1036,
  [SMALL_STATE(91)] = 1043,
};

static const TSParseActionEntry ts_parse_actions[] = {
  [0] = {.entry = {.count = 0, .reusable = false}},
  [1] = {.entry = {.count = 1, .reusable = false}}, RECOVER(),
  [3] = {.entry = {.count = 1, .reusable = true}}, SHIFT_EXTRA(),
  [5] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_snakemake, 0),
  [7] = {.entry = {.count = 1, .reusable = true}}, SHIFT(81),
  [9] = {.entry = {.count = 1, .reusable = true}}, SHIFT(47),
  [11] = {.entry = {.count = 1, .reusable = true}}, SHIFT(48),
  [13] = {.entry = {.count = 1, .reusable = true}}, SHIFT(88),
  [15] = {.entry = {.count = 1, .reusable = true}}, SHIFT(67),
  [17] = {.entry = {.count = 1, .reusable = true}}, SHIFT(75),
  [19] = {.entry = {.count = 1, .reusable = true}}, SHIFT(74),
  [21] = {.entry = {.count = 1, .reusable = true}}, SHIFT(72),
  [23] = {.entry = {.count = 1, .reusable = true}}, SHIFT(71),
  [25] = {.entry = {.count = 1, .reusable = true}}, SHIFT(80),
  [27] = {.entry = {.count = 1, .reusable = true}}, SHIFT(66),
  [29] = {.entry = {.count = 1, .reusable = true}}, SHIFT(68),
  [31] = {.entry = {.count = 1, .reusable = true}}, SHIFT(59),
  [33] = {.entry = {.count = 1, .reusable = true}}, SHIFT(84),
  [35] = {.entry = {.count = 1, .reusable = true}}, SHIFT(91),
  [37] = {.entry = {.count = 1, .reusable = true}}, SHIFT(85),
  [39] = {.entry = {.count = 1, .reusable = true}}, SHIFT(89),
  [41] = {.entry = {.count = 1, .reusable = true}}, SHIFT(90),
  [43] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym__ni, 2),
  [45] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_snakemake, 1),
  [47] = {.entry = {.count = 1, .reusable = true}}, REDUCE(aux_sym_snakemake_repeat1, 2),
  [49] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_snakemake_repeat1, 2), SHIFT_REPEAT(81),
  [52] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_snakemake_repeat1, 2), SHIFT_REPEAT(47),
  [55] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_snakemake_repeat1, 2), SHIFT_REPEAT(48),
  [58] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_snakemake_repeat1, 2), SHIFT_REPEAT(88),
  [61] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_snakemake_repeat1, 2), SHIFT_REPEAT(67),
  [64] = {.entry = {.count = 1, .reusable = false}}, SHIFT_EXTRA(),
  [66] = {.entry = {.count = 2, .reusable = false}}, REDUCE(aux_sym_string_repeat1, 2), SHIFT_REPEAT(15),
  [69] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_string_repeat1, 2), SHIFT_REPEAT(15),
  [72] = {.entry = {.count = 1, .reusable = true}}, REDUCE(aux_sym_string_repeat1, 2),
  [74] = {.entry = {.count = 1, .reusable = false}}, SHIFT(15),
  [76] = {.entry = {.count = 1, .reusable = true}}, SHIFT(15),
  [78] = {.entry = {.count = 1, .reusable = true}}, SHIFT(51),
  [80] = {.entry = {.count = 1, .reusable = true}}, SHIFT(52),
  [82] = {.entry = {.count = 1, .reusable = true}}, SHIFT(53),
  [84] = {.entry = {.count = 1, .reusable = true}}, SHIFT(54),
  [86] = {.entry = {.count = 1, .reusable = true}}, SHIFT(14),
  [88] = {.entry = {.count = 1, .reusable = true}}, SHIFT(16),
  [90] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym__ruleparams, 2, .production_id = 4),
  [92] = {.entry = {.count = 1, .reusable = true}}, SHIFT(3),
  [94] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_string, 2),
  [96] = {.entry = {.count = 1, .reusable = false}}, REDUCE(aux_sym_string_repeat1, 1),
  [98] = {.entry = {.count = 1, .reusable = true}}, REDUCE(aux_sym_string_repeat1, 1),
  [100] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_string, 3, .production_id = 2),
  [102] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym__workdir, 2),
  [104] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym__configfile, 3),
  [106] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_container, 3),
  [108] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym__include, 2),
  [110] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_rule, 4, .production_id = 1),
  [112] = {.entry = {.count = 1, .reusable = true}}, SHIFT(8),
  [114] = {.entry = {.count = 1, .reusable = true}}, SHIFT(60),
  [116] = {.entry = {.count = 1, .reusable = true}}, SHIFT(10),
  [118] = {.entry = {.count = 1, .reusable = true}}, SHIFT(33),
  [120] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_output, 4, .production_id = 12),
  [122] = {.entry = {.count = 1, .reusable = true}}, REDUCE(aux_sym__shellparams_repeat1, 2),
  [124] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym__shellparams_repeat1, 2), SHIFT_REPEAT(30),
  [127] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym_input_repeat1, 2), SHIFT_REPEAT(33),
  [130] = {.entry = {.count = 1, .reusable = true}}, REDUCE(aux_sym_input_repeat1, 2),
  [132] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym__shellparams, 2),
  [134] = {.entry = {.count = 1, .reusable = true}}, SHIFT(30),
  [136] = {.entry = {.count = 1, .reusable = true}}, SHIFT(13),
  [138] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_conda, 4, .production_id = 13),
  [140] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_input, 4, .production_id = 11),
  [142] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym__shellparams, 1),
  [144] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_conda, 3, .production_id = 9),
  [146] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_output, 3, .production_id = 7),
  [148] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_input, 3, .production_id = 6),
  [150] = {.entry = {.count = 1, .reusable = true}}, SHIFT(61),
  [152] = {.entry = {.count = 2, .reusable = true}}, REDUCE(aux_sym__norunparams, 2, .production_id = 5), SHIFT_REPEAT(60),
  [155] = {.entry = {.count = 1, .reusable = true}}, SHIFT(11),
  [157] = {.entry = {.count = 1, .reusable = true}}, SHIFT(39),
  [159] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_boolean, 1),
  [161] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_cache, 3),
  [163] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_script, 3),
  [165] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_threads, 3),
  [167] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym__resources, 3),
  [169] = {.entry = {.count = 1, .reusable = true}}, SHIFT(50),
  [171] = {.entry = {.count = 1, .reusable = true}}, SHIFT(45),
  [173] = {.entry = {.count = 1, .reusable = true}}, SHIFT(87),
  [175] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_message, 3),
  [177] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_notebook, 3),
  [179] = {.entry = {.count = 1, .reusable = true}}, SHIFT(41),
  [181] = {.entry = {.count = 1, .reusable = true}}, SHIFT(42),
  [183] = {.entry = {.count = 1, .reusable = true}}, SHIFT(23),
  [185] = {.entry = {.count = 1, .reusable = true}}, SHIFT(24),
  [187] = {.entry = {.count = 1, .reusable = true}}, SHIFT(43),
  [189] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_log, 3),
  [191] = {.entry = {.count = 1, .reusable = true}}, REDUCE(aux_sym__norunparams, 2),
  [193] = {.entry = {.count = 1, .reusable = true}}, REDUCE(aux_sym__norunparams, 2, .production_id = 3),
  [195] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym__params, 3, .production_id = 8),
  [197] = {.entry = {.count = 1, .reusable = true}}, SHIFT(40),
  [199] = {.entry = {.count = 1, .reusable = true}}, SHIFT(73),
  [201] = {.entry = {.count = 1, .reusable = true}},  ACCEPT_INPUT(),
  [203] = {.entry = {.count = 1, .reusable = true}}, SHIFT(38),
  [205] = {.entry = {.count = 1, .reusable = true}}, SHIFT(22),
  [207] = {.entry = {.count = 1, .reusable = true}}, REDUCE(sym_shell, 3, .production_id = 10),
  [209] = {.entry = {.count = 1, .reusable = true}}, SHIFT(64),
  [211] = {.entry = {.count = 1, .reusable = true}}, SHIFT(57),
  [213] = {.entry = {.count = 1, .reusable = true}}, SHIFT(55),
  [215] = {.entry = {.count = 1, .reusable = true}}, SHIFT(56),
  [217] = {.entry = {.count = 1, .reusable = true}}, SHIFT(49),
};

#ifdef __cplusplus
extern "C" {
#endif
void *tree_sitter_snakemake_external_scanner_create(void);
void tree_sitter_snakemake_external_scanner_destroy(void *);
bool tree_sitter_snakemake_external_scanner_scan(void *, TSLexer *, const bool *);
unsigned tree_sitter_snakemake_external_scanner_serialize(void *, char *);
void tree_sitter_snakemake_external_scanner_deserialize(void *, const char *, unsigned);

#ifdef _WIN32
#define extern __declspec(dllexport)
#endif

extern const TSLanguage *tree_sitter_snakemake(void) {
  static const TSLanguage language = {
    .version = LANGUAGE_VERSION,
    .symbol_count = SYMBOL_COUNT,
    .alias_count = ALIAS_COUNT,
    .token_count = TOKEN_COUNT,
    .external_token_count = EXTERNAL_TOKEN_COUNT,
    .state_count = STATE_COUNT,
    .large_state_count = LARGE_STATE_COUNT,
    .production_id_count = PRODUCTION_ID_COUNT,
    .field_count = FIELD_COUNT,
    .max_alias_sequence_length = MAX_ALIAS_SEQUENCE_LENGTH,
    .parse_table = &ts_parse_table[0][0],
    .small_parse_table = ts_small_parse_table,
    .small_parse_table_map = ts_small_parse_table_map,
    .parse_actions = ts_parse_actions,
    .symbol_names = ts_symbol_names,
    .field_names = ts_field_names,
    .field_map_slices = ts_field_map_slices,
    .field_map_entries = ts_field_map_entries,
    .symbol_metadata = ts_symbol_metadata,
    .public_symbol_map = ts_symbol_map,
    .alias_map = ts_non_terminal_alias_map,
    .alias_sequences = &ts_alias_sequences[0][0],
    .lex_modes = ts_lex_modes,
    .lex_fn = ts_lex,
    .external_scanner = {
      &ts_external_scanner_states[0][0],
      ts_external_scanner_symbol_map,
      tree_sitter_snakemake_external_scanner_create,
      tree_sitter_snakemake_external_scanner_destroy,
      tree_sitter_snakemake_external_scanner_scan,
      tree_sitter_snakemake_external_scanner_serialize,
      tree_sitter_snakemake_external_scanner_deserialize,
    },
  };
  return &language;
}
#ifdef __cplusplus
}
#endif
