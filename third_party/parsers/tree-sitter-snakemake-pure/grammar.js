const PREC = {
  // this resolves a conflict between the usage of ':' in a lambda vs in a
  // typed parameter. In the case of a lambda, we don't allow typed parameters.
  lambda: -2,
  typed_parameter: -1,
  conditional: -1,

  expression_statement: -2,
  rule: -1,

  parenthesized_expression: 1,
  parenthesized_list_splat: 1,
  not: 1,
  compare: 2,
  or: 10,
  and: 11,
  bitwise_or: 12,
  bitwise_and: 13,
  xor: 14,
  shift: 15,
  plus: 16,
  times: 17,
  unary: 18,
  power: 19,
  call: 20,
}

const SEMICOLON = ';'

module.exports = grammar({
  name: 'snakemake',

  extras: $ => [
    $.comment,
    /[\s\f\uFEFF\u2060\u200B]|\\\r?\n/
  ],

  conflicts: $ => [
    [$._norunparams],
    [$._shellparams]
  ],

  externals: $ => [
    $._newline,
    $._indent,
    $._dedent,
    $._string_start,
    $._string_content,
    $._string_end,
  ],


rules: {
    snakemake: $ => repeat(choice(
      // $._statement,
      $.rule,
      $._include,
      $._workdir,
      $._configfile,
      $.container
    )),

    rule: $ => prec(PREC.rule, seq(
      'rule',
      field('name',$.identifier),
      ':',
      //field('body',$._ruleparams)
      $._ruleparams
    )),


    _include: $ => seq('include:', $.string),

    _workdir: $ => seq('workdir:', $.string),

    _module: $ => seq('module', $.identifier, ':' ,$._moduleparams),

    _configfile: $ => seq('configfile', ':' ,$.string),

    _userule: $ => seq(
      'use',
      'rule',
      choice($.identifier, '*'),
      'from', $.identifier,
      optional(seq('as', $.identifier)),
      optional(seq('with', $._norunparams))),

    _ni: $ => seq($._newline, $._indent),

    _norunparams: $ => repeat1(seq($._ni, choice(
      $.input,
      $.output,
      $._params,
      $.message,
      $.threads,
      $._resources,
      $.log,
      $.conda,
      $.container,
      // $.benchmark,
      //$.run,
      $.cache,
      $.shell,
      $.script,
      $.notebook
    ))),

    _ruleparams: $ => seq($._norunparams, $._newline),


    input: $ => seq('input', ':', field('input', commaSep1($._parameter_list))),

    output: $ => seq('output', ':', field('output', commaSep1($._parameter_list))),

    _params: $ => seq('params', ':', field('params', $._parameter_list)),

    log: $ => seq('log', ':', $._parameter_list),

    //benchmark: $ => seq('benchmark', ':', $._statement),

    cache: $ => seq('cache', ':', $.boolean),

    
    message: $ => seq('message', ':', $.string),

    threads: $ => seq('threads', ':', $.integer),

    _resources: $ => seq('resources', ':', $._parameter_list),

    // _version: $ => seq('version', ':', $._statement),

    conda: $ => seq('conda', ':', field('conda', sep1($._parameter_list, ','))),

    container: $ => seq('container', ':', $.string),

    //run: $ => seq('run', '', $._ni, $._statement),

    shell: $ => seq('shell', ':', field('shell', $._shellparams)),
    
    //_shellparams: $ => repeat1(seq($._ni, $._parameter_list)),
    _shellparams: $ => sep1($._parameter_list, $._indent),

    script: $ => seq('script', ':', $.string),

    notebook: $ => seq('notebook', ':', $.string),

    _moduleparams: $ => seq('', optional(seq($._ni, $._snakefile)), optional(seq($._ni, $._metawrapper)), optional(seq($._ni, $._config)), optional(seq($._ni, $._skipval))),

    _snakefile: $ => seq('snakefile', ':', $.string),

    _metawrapper: $ => seq('meta_wrapper', ':', $.string),

    _config: $ => seq('config', ':', $.string),

    _skipval: $ => seq('skip_validation', ':', $.string),
    
    _parameter_list: $ => $.string,

    comment: $ => token(seq('#', /.*/)),

    identifier: $ => /[_\p{XID_Start}][_\p{XID_Continue}]*/,

    string: $ => seq(
      alias($._string_start, '"'),
      alias(repeat(choice($._escape_interpolation, $.escape_sequence, $._not_escape_sequence, $._string_content)), 'content'),
      alias($._string_end, '"')
    ),

    _escape_interpolation: $ => choice('{{', '}}'),

    escape_sequence: $ => token(prec(1, seq(
      '\\',
      choice(
        /u[a-fA-F\d]{4}/,
        /U[a-fA-F\d]{8}/,
        /x[a-fA-F\d]{2}/,
        /\d{3}/,
        /\r?\n/,
        /['"abfrntv\\]/,
      )
    ))),

    _not_escape_sequence: $ => '\\',

    boolean: $ => choice('True', 'False'),

    integer: $ => token(choice(
      seq(
        choice('0x', '0X'),
        repeat1(/_?[A-Fa-f0-9]+/),
        optional(/[Ll]/)
      ),
      seq(
        choice('0o', '0O'),
        repeat1(/_?[0-7]+/),
        optional(/[Ll]/)
      ),
      seq(
        choice('0b', '0B'),
        repeat1(/_?[0-1]+/),
        optional(/[Ll]/)
      ),
      seq(
        repeat1(/[0-9]+_?/),
        choice(
          optional(/[Ll]/), // long numbers
          optional(/[jJ]/) // complex numbers
        )
      )
    )),
  }
})

function commaSep1(rule) {
  return sep1(rule, ',')
}


function sep1(rule, separator) {
  return seq(rule, repeat(seq(separator, rule)))
}
