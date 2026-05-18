//! Operator resolution — determines how to execute each pipeline node.
//!
//! Ports the routing logic from `dorian/pipeline/operator_resolver.py` to Rust.
//! The actual execution happens in the dispatch crate's Runtime implementations;
//! this module only decides WHAT to call and WHERE to dispatch.
//!
//! Resolution categories:
//! - **Library operators** (sklearn, pandas, guardrails): Python runtime
//! - **Snippets** (user-defined code): Python runtime (sandboxed), future: WASM
//! - **Evaluation procedures** (embedded DAG): Python runtime
//! - **Ranking objectives** (`def score(...)`): Python runtime, future: WASM
//! - **LLM operators** (openrouter.*): API runtime
//! - **Parameters** (safe type casting): Engine-native, no runtime needed
//! - **Platform operators** (dorian.io.*): Must be expanded before resolution

use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};

use super::client::KbClient;
use super::queries::KbError;

// ---------------------------------------------------------------------------
// Resolution result types
// ---------------------------------------------------------------------------

/// The runtime target for a resolved operator.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum RuntimeTarget {
    /// Python subprocess runtime (library operators, snippets, eval procedures).
    Python,
    /// API/HTTP runtime (LLM operators, external services).
    Api,
    /// Engine-native resolution (parameters — no runtime call needed).
    EngineNative,
    /// WASM sandbox (future: user-defined snippets/objectives).
    Wasm,
    /// Container runtime (future: heavy operators).
    Container,
}

/// How to invoke the operator within its target runtime.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum InvocationKind {
    /// Dotted Python import path → instantiate/call.
    /// E.g., "sklearn.ensemble.RandomForestClassifier"
    DottedImport {
        /// The actual import path (may differ from operator name due to KB remapping).
        import_path: String,
        /// Whether the resolved object is a class (needs instantiation).
        is_class: bool,
    },

    /// Method shortcut — dispatch as `getattr(instance, method_name)`.
    /// E.g., "fit", "transform", "predict"
    MethodShortcut {
        method_name: String,
    },

    /// User-defined snippet — restricted exec environment.
    Snippet {
        code: String,
        language: String,
    },

    /// Parameter — safe type coercion, no runtime needed.
    Parameter {
        dtype: String,
        value: String,
    },

    /// API call — HTTP/gRPC to external service.
    ApiCall {
        endpoint: String,
    },
}

/// Full resolution result for a pipeline node.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Resolution {
    /// Which runtime should execute this node.
    pub target: RuntimeTarget,
    /// How to invoke the operator within the runtime.
    pub kind: InvocationKind,
    /// Whether this node produces multiple outputs (e.g., method shortcuts → (instance, result)).
    pub multi_output: bool,
}

// ---------------------------------------------------------------------------
// Resolver
// ---------------------------------------------------------------------------

/// Stateful resolver with KB-backed method shortcuts and import remapping.
pub struct OperatorResolver {
    /// Method names from KB `calls` chains (e.g., "fit", "transform", "predict").
    method_shortcuts: HashSet<String>,
    /// Import path remappings from KB `is_subclass_of`.
    import_remap: HashMap<String, String>,
    /// Library → pip package map.
    library_map: HashMap<String, String>,
}

impl OperatorResolver {
    /// Create a resolver pre-loaded with KB data.
    pub async fn from_kb(kb: &KbClient) -> Result<Self, KbError> {
        let methods = kb.get_all_interface_methods().await?;
        let lib_map = kb.get_library_package_map().await?;

        Ok(Self {
            method_shortcuts: methods.into_iter().collect(),
            import_remap: HashMap::new(), // populated lazily
            library_map: lib_map,
        })
    }

    /// Create a resolver with pre-populated data (for testing without KB).
    pub fn new(
        method_shortcuts: HashSet<String>,
        import_remap: HashMap<String, String>,
        library_map: HashMap<String, String>,
    ) -> Self {
        Self {
            method_shortcuts,
            import_remap,
            library_map,
        }
    }

    /// Resolve a pipeline node to a runtime target and invocation kind.
    ///
    /// Node types:
    /// - Operator: FQN like "sklearn.ensemble.RandomForestClassifier"
    /// - Snippet: inline code with `foo(...)` entry point
    /// - Parameter: safe type coercion
    pub fn resolve(
        &self,
        node_type: &str,
        node_name: &str,
        node_language: Option<&str>,
        node_code: Option<&str>,
        param_dtype: Option<&str>,
        param_value: Option<&str>,
    ) -> Resolution {
        match node_type {
            "parameter" => self.resolve_parameter(param_dtype, param_value),
            "snippet" => self.resolve_snippet(node_code, node_language),
            "operator" => self.resolve_operator(node_name),
            _ => {
                // Unknown node type — default to Python.
                Resolution {
                    target: RuntimeTarget::Python,
                    kind: InvocationKind::DottedImport {
                        import_path: node_name.to_string(),
                        is_class: false,
                    },
                    multi_output: false,
                }
            }
        }
    }

    /// Resolve a parameter node — engine-native, no runtime needed.
    fn resolve_parameter(
        &self,
        dtype: Option<&str>,
        value: Option<&str>,
    ) -> Resolution {
        Resolution {
            target: RuntimeTarget::EngineNative,
            kind: InvocationKind::Parameter {
                dtype: dtype.unwrap_or("string").to_string(),
                value: value.unwrap_or("").to_string(),
            },
            multi_output: false,
        }
    }

    /// Resolve a snippet node — Python sandbox.
    fn resolve_snippet(
        &self,
        code: Option<&str>,
        language: Option<&str>,
    ) -> Resolution {
        Resolution {
            target: RuntimeTarget::Python,
            kind: InvocationKind::Snippet {
                code: code.unwrap_or("").to_string(),
                language: language.unwrap_or("python").to_string(),
            },
            multi_output: false,
        }
    }

    /// Resolve an operator node — routing based on FQN and KB data.
    fn resolve_operator(&self, name: &str) -> Resolution {
        // 1. Check if it's a method shortcut (e.g., "fit", "transform").
        if self.method_shortcuts.contains(name) {
            return Resolution {
                target: RuntimeTarget::Python,
                kind: InvocationKind::MethodShortcut {
                    method_name: name.to_string(),
                },
                multi_output: true, // method shortcuts always return (instance, result)
            };
        }

        // 2. Check if it's a platform operator (should be expanded before resolution).
        if name.starts_with("dorian.") {
            tracing::warn!(
                operator = %name,
                "platform operator reached resolution — should have been expanded"
            );
        }

        // 3. Check if it's an API operator (e.g., openrouter.*).
        if name.starts_with("openrouter.") {
            return Resolution {
                target: RuntimeTarget::Api,
                kind: InvocationKind::ApiCall {
                    endpoint: name.to_string(),
                },
                multi_output: false,
            };
        }

        // 4. Dotted import (Python library operator).
        let import_path = self
            .import_remap
            .get(name)
            .cloned()
            .unwrap_or_else(|| name.to_string());

        // Heuristic: names with dots that end in a CamelCase segment are likely classes.
        let is_class = import_path.contains('.')
            && import_path
                .rsplit('.')
                .next()
                .map(|last| last.chars().next().map(|c| c.is_uppercase()).unwrap_or(false))
                .unwrap_or(false);

        Resolution {
            target: RuntimeTarget::Python,
            kind: InvocationKind::DottedImport {
                import_path,
                is_class,
            },
            multi_output: false,
        }
    }

    /// Build a runtime map for an entire pipeline: node_id → RuntimeTarget.
    ///
    /// Used by the dispatcher to pre-allocate runtime slots.
    pub fn build_runtime_map<'a>(
        &self,
        nodes: impl Iterator<Item = (&'a str, &'a str, &'a str)>,
    ) -> HashMap<String, RuntimeTarget> {
        nodes
            .map(|(id, node_type, name)| {
                let resolution = self.resolve(node_type, name, None, None, None, None);
                (id.to_string(), resolution.target)
            })
            .collect()
    }

    /// Check if a name is a known method shortcut.
    pub fn is_method_shortcut(&self, name: &str) -> bool {
        self.method_shortcuts.contains(name)
    }

    /// Get the pip package name for a library import.
    pub fn get_package_for_import(&self, import_name: &str) -> Option<&str> {
        // Extract top-level module name.
        let top_level = import_name.split('.').next().unwrap_or(import_name);
        self.library_map.get(top_level).map(|s| s.as_str())
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn test_resolver() -> OperatorResolver {
        let shortcuts: HashSet<String> = ["fit", "transform", "predict", "fit_transform", "create", "validate"]
            .iter()
            .map(|s| s.to_string())
            .collect();

        let mut remap = HashMap::new();
        remap.insert(
            "sklearn.ensemble.RandomForestClassifier".to_string(),
            "sklearn.ensemble._forest.RandomForestClassifier".to_string(),
        );

        let mut lib_map = HashMap::new();
        lib_map.insert("sklearn".to_string(), "scikit-learn".to_string());
        lib_map.insert("pandas".to_string(), "pandas".to_string());

        OperatorResolver::new(shortcuts, remap, lib_map)
    }

    #[test]
    fn test_resolve_parameter() {
        let resolver = test_resolver();
        let res = resolver.resolve("parameter", "n_estimators", None, None, Some("int"), Some("100"));
        assert_eq!(res.target, RuntimeTarget::EngineNative);
        assert!(!res.multi_output);
        match res.kind {
            InvocationKind::Parameter { dtype, value } => {
                assert_eq!(dtype, "int");
                assert_eq!(value, "100");
            }
            _ => panic!("expected Parameter"),
        }
    }

    #[test]
    fn test_resolve_snippet() {
        let resolver = test_resolver();
        let res = resolver.resolve("snippet", "my_code", Some("python"), Some("def foo(x): return x*2"), None, None);
        assert_eq!(res.target, RuntimeTarget::Python);
        match res.kind {
            InvocationKind::Snippet { code, language } => {
                assert_eq!(code, "def foo(x): return x*2");
                assert_eq!(language, "python");
            }
            _ => panic!("expected Snippet"),
        }
    }

    #[test]
    fn test_resolve_method_shortcut() {
        let resolver = test_resolver();
        let res = resolver.resolve("operator", "fit", None, None, None, None);
        assert_eq!(res.target, RuntimeTarget::Python);
        assert!(res.multi_output);
        match res.kind {
            InvocationKind::MethodShortcut { method_name } => {
                assert_eq!(method_name, "fit");
            }
            _ => panic!("expected MethodShortcut"),
        }
    }

    #[test]
    fn test_resolve_dotted_operator() {
        let resolver = test_resolver();
        let res = resolver.resolve("operator", "sklearn.ensemble.RandomForestClassifier", None, None, None, None);
        assert_eq!(res.target, RuntimeTarget::Python);
        assert!(!res.multi_output);
        match res.kind {
            InvocationKind::DottedImport { import_path, is_class } => {
                // Should use remapped path.
                assert_eq!(import_path, "sklearn.ensemble._forest.RandomForestClassifier");
                assert!(is_class);
            }
            _ => panic!("expected DottedImport"),
        }
    }

    #[test]
    fn test_resolve_function_operator() {
        let resolver = test_resolver();
        let res = resolver.resolve("operator", "pandas.read_csv", None, None, None, None);
        assert_eq!(res.target, RuntimeTarget::Python);
        match res.kind {
            InvocationKind::DottedImport { import_path, is_class } => {
                assert_eq!(import_path, "pandas.read_csv");
                assert!(!is_class); // lowercase = function, not class
            }
            _ => panic!("expected DottedImport"),
        }
    }

    #[test]
    fn test_resolve_api_operator() {
        let resolver = test_resolver();
        let res = resolver.resolve("operator", "openrouter.chat.completion", None, None, None, None);
        assert_eq!(res.target, RuntimeTarget::Api);
        match res.kind {
            InvocationKind::ApiCall { endpoint } => {
                assert_eq!(endpoint, "openrouter.chat.completion");
            }
            _ => panic!("expected ApiCall"),
        }
    }

    #[test]
    fn test_is_method_shortcut() {
        let resolver = test_resolver();
        assert!(resolver.is_method_shortcut("fit"));
        assert!(resolver.is_method_shortcut("transform"));
        assert!(!resolver.is_method_shortcut("sklearn.ensemble.RandomForestClassifier"));
    }

    #[test]
    fn test_get_package_for_import() {
        let resolver = test_resolver();
        assert_eq!(resolver.get_package_for_import("sklearn.ensemble"), Some("scikit-learn"));
        assert_eq!(resolver.get_package_for_import("pandas"), Some("pandas"));
        assert_eq!(resolver.get_package_for_import("unknown_lib"), None);
    }

    #[test]
    fn test_build_runtime_map() {
        let resolver = test_resolver();
        let nodes = vec![
            ("n1", "operator", "sklearn.ensemble.RandomForestClassifier"),
            ("n2", "parameter", "n_estimators"),
            ("n3", "operator", "fit"),
            ("n4", "snippet", "my_code"),
            ("n5", "operator", "openrouter.chat.completion"),
        ];

        let map = resolver.build_runtime_map(nodes.into_iter());
        assert_eq!(map.get("n1"), Some(&RuntimeTarget::Python));
        assert_eq!(map.get("n2"), Some(&RuntimeTarget::EngineNative));
        assert_eq!(map.get("n3"), Some(&RuntimeTarget::Python));
        assert_eq!(map.get("n4"), Some(&RuntimeTarget::Python));
        assert_eq!(map.get("n5"), Some(&RuntimeTarget::Api));
    }

    #[test]
    fn test_resolve_unknown_type() {
        let resolver = test_resolver();
        let res = resolver.resolve("group", "my_group", None, None, None, None);
        assert_eq!(res.target, RuntimeTarget::Python);
    }

    #[test]
    fn test_runtime_target_serialization() {
        let target = RuntimeTarget::Python;
        let json = serde_json::to_string(&target).unwrap();
        assert_eq!(json, "\"Python\"");
    }
}
