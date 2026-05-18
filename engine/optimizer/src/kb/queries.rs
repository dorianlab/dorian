//! Neo4j Cypher queries for the Dorian knowledge base.
//!
//! Each function maps 1:1 to a Python function in `dorian/knowledge/queries.py`.
//! All queries are parameterized (no string interpolation) and results are cached
//! in the client's LRU caches.

use std::collections::HashMap;

use neo4rs::query;

use super::client::KbClient;
use super::types::*;

/// KB query error type combining Neo4j and deserialization errors.
#[derive(Debug, thiserror::Error)]
pub enum KbError {
    #[error("neo4j: {0}")]
    Neo4j(#[from] neo4rs::Error),
    #[error("deserialization: {0}")]
    Deserialize(String),
}

// ---------------------------------------------------------------------------
// Operator resolution queries
// ---------------------------------------------------------------------------

impl KbClient {
    /// Get the interface name for an operator (e.g., "Sklearn Transformer").
    pub async fn get_operator_interface(
        &self,
        operator_name: &str,
    ) -> Result<Option<String>, KbError> {
        {
            let mut cache = self.cache_interface.lock().await;
            if let Some(cached) = cache.get(&operator_name.to_string()) {
                return Ok(cached.clone());
            }
        }

        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH (op)-[:is_a]->(iface)-[:is_an]->(:Node {name: 'Interface'}) \
                     WHERE op.name = $name \
                     RETURN iface.name AS interface_name",
                )
                .param("name", operator_name),
            )
            .await?;

        let interface: Option<String> = match result.next().await? {
            Some(row) => row.get("interface_name").ok(),
            None => None,
        };

        {
            let mut cache = self.cache_interface.lock().await;
            cache.put(operator_name.to_string(), interface.clone());
        }

        Ok(interface)
    }

    /// Get the Python import path for an operator.
    ///
    /// Some operators have a remapped import path via `is_subclass_of`.
    pub async fn get_operator_import_path(
        &self,
        operator_name: &str,
    ) -> Result<Option<String>, KbError> {
        {
            let mut cache = self.cache_import_path.lock().await;
            if let Some(cached) = cache.get(&operator_name.to_string()) {
                return Ok(cached.clone());
            }
        }

        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH (op)-[:is_subclass_of]->(target) \
                     WHERE op.name = $name AND target.name CONTAINS '.' \
                     RETURN target.name AS import_path",
                )
                .param("name", operator_name),
            )
            .await?;

        let path: Option<String> = match result.next().await? {
            Some(row) => row.get("import_path").ok(),
            None => None,
        };

        {
            let mut cache = self.cache_import_path.lock().await;
            cache.put(operator_name.to_string(), path.clone());
        }

        Ok(path)
    }

    /// Get the ordered method sequence for an interface.
    ///
    /// Returns method names in execution order (by chain depth).
    /// E.g., for "Sklearn Transformer": ["__init__", "fit", "transform"]
    pub async fn get_method_sequence(
        &self,
        interface_name: &str,
    ) -> Result<Vec<String>, KbError> {
        {
            let mut cache = self.cache_method_seq.lock().await;
            if let Some(cached) = cache.get(&interface_name.to_string()) {
                return Ok(cached.clone());
            }
        }

        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH path = (n)-[:calls*]->(m:Method) \
                     WHERE n.name = $name \
                     RETURN m.name AS method_name, length(path) AS depth \
                     ORDER BY depth ASC",
                )
                .param("name", interface_name),
            )
            .await?;

        let mut methods = Vec::new();
        while let Ok(Some(row)) = result.next().await {
            if let Ok(name) = row.get::<String>("method_name") {
                methods.push(name);
            }
        }

        {
            let mut cache = self.cache_method_seq.lock().await;
            cache.put(interface_name.to_string(), methods.clone());
        }

        Ok(methods)
    }

    // ---------------------------------------------------------------------------
    // Parameter metadata queries
    // ---------------------------------------------------------------------------

    /// Get parameters for an operator, with inheritance from interface/method levels.
    ///
    /// Priority: When the same parameter name appears at multiple levels,
    /// the "richer" definition (more annotations) wins.
    pub async fn get_operator_parameters(
        &self,
        operator_name: &str,
    ) -> Result<Vec<ParameterSpec>, KbError> {
        {
            let mut cache = self.cache_params.lock().await;
            if let Some(cached) = cache.get(&operator_name.to_string()) {
                return Ok(cached.clone());
            }
        }

        // Query operator-level params.
        let mut params_by_name: HashMap<String, ParameterSpec> = HashMap::new();

        // 1. Operator-level parameters.
        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH (op)-[:has_parameter]->(p) \
                     WHERE op.name = $name \
                     RETURN p.name AS param_name, p.type AS param_type, \
                            p.default AS param_default, p.low AS param_low, \
                            p.high AS param_high, p.choices AS param_choices, \
                            p.log_scale AS param_log_scale",
                )
                .param("name", operator_name),
            )
            .await?;

        while let Ok(Some(row)) = result.next().await {
            if let Some(spec) = row_to_param_spec(&row, None) {
                merge_param(&mut params_by_name, spec);
            }
        }

        // 2. Interface-level parameters.
        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH (op)-[:is_a]->(iface)-[:has_parameter]->(p) \
                     WHERE op.name = $name \
                     RETURN p.name AS param_name, p.type AS param_type, \
                            p.default AS param_default, p.low AS param_low, \
                            p.high AS param_high, p.choices AS param_choices, \
                            p.log_scale AS param_log_scale",
                )
                .param("name", operator_name),
            )
            .await?;

        while let Ok(Some(row)) = result.next().await {
            if let Some(spec) = row_to_param_spec(&row, None) {
                merge_param(&mut params_by_name, spec);
            }
        }

        // 3. Method-level parameters.
        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH (op)-[:is_a]->(iface)-[:calls*]->(m:Method)-[:has_parameter]->(p) \
                     WHERE op.name = $name \
                     RETURN p.name AS param_name, p.type AS param_type, \
                            p.default AS param_default, p.low AS param_low, \
                            p.high AS param_high, p.choices AS param_choices, \
                            p.log_scale AS param_log_scale, m.name AS method_name",
                )
                .param("name", operator_name),
            )
            .await?;

        while let Ok(Some(row)) = result.next().await {
            let method: Option<String> = row.get("method_name").ok();
            if let Some(spec) = row_to_param_spec(&row, method) {
                merge_param(&mut params_by_name, spec);
            }
        }

        let params: Vec<ParameterSpec> = params_by_name.into_values().collect();

        {
            let mut cache = self.cache_params.lock().await;
            cache.put(operator_name.to_string(), params.clone());
        }

        Ok(params)
    }

    /// Get I/O specification for an interface (inputs and outputs).
    pub async fn get_interface_io(
        &self,
        interface_name: &str,
    ) -> Result<(Vec<IoSpec>, Vec<IoSpec>), KbError> {
        {
            let mut cache = self.cache_io.lock().await;
            if let Some(cached) = cache.get(&interface_name.to_string()) {
                return Ok(cached.clone());
            }
        }

        let inputs = self.query_io_ports(interface_name, "has_input").await?;
        let outputs = self.query_io_ports(interface_name, "has_output").await?;

        let io = (inputs, outputs);
        {
            let mut cache = self.cache_io.lock().await;
            cache.put(interface_name.to_string(), io.clone());
        }

        Ok(io)
    }

    /// Get per-method I/O specifications for an interface.
    pub async fn get_method_io(
        &self,
        interface_name: &str,
    ) -> Result<MethodIo, KbError> {
        {
            let mut cache = self.cache_method_io.lock().await;
            if let Some(cached) = cache.get(&interface_name.to_string()) {
                return Ok(cached.clone());
            }
        }

        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH (n)-[:calls*]->(m:Method) \
                     WHERE n.name = $name \
                     OPTIONAL MATCH (m)-[:has_input]->(inp) \
                     OPTIONAL MATCH (inp)-[:is_of_type]->(inp_t) \
                     OPTIONAL MATCH (inp_t)-[:has_position]->(inp_pos) \
                     OPTIONAL MATCH (m)-[:has_output]->(outp) \
                     OPTIONAL MATCH (outp)-[:is_of_type]->(outp_t) \
                     OPTIONAL MATCH (outp_t)-[:has_position]->(outp_pos) \
                     RETURN m.name AS method_name, \
                            inp.name AS inp_name, inp_t.name AS inp_type, inp_pos.name AS inp_pos, \
                            outp.name AS outp_name, outp_t.name AS outp_type, outp_pos.name AS outp_pos",
                )
                .param("name", interface_name),
            )
            .await?;

        let mut method_io: MethodIo = HashMap::new();

        while let Ok(Some(row)) = result.next().await {
            let method_name: String = match row.get("method_name") {
                Ok(n) => n,
                Err(_) => continue,
            };

            let entry = method_io
                .entry(method_name)
                .or_insert_with(|| (Vec::new(), Vec::new()));

            if let Ok(inp_name) = row.get::<String>("inp_name") {
                let io = IoSpec {
                    name: inp_name.clone(),
                    dtype: row.get::<String>("inp_type").unwrap_or_else(|_| "any".to_string()),
                    position: row
                        .get::<String>("inp_pos")
                        .unwrap_or_else(|_| "0".to_string()),
                };
                if !entry.0.iter().any(|e| e.name == inp_name) {
                    entry.0.push(io);
                }
            }

            if let Ok(outp_name) = row.get::<String>("outp_name") {
                let io = IoSpec {
                    name: outp_name.clone(),
                    dtype: row.get::<String>("outp_type").unwrap_or_else(|_| "any".to_string()),
                    position: row
                        .get::<String>("outp_pos")
                        .unwrap_or_else(|_| "0".to_string()),
                };
                if !entry.1.iter().any(|e| e.name == outp_name) {
                    entry.1.push(io);
                }
            }
        }

        {
            let mut cache = self.cache_method_io.lock().await;
            cache.put(interface_name.to_string(), method_io.clone());
        }

        Ok(method_io)
    }

    /// Get interface attributes (e.g., "passthrough" for guardrails).
    pub async fn get_interface_attributes(
        &self,
        interface_name: &str,
    ) -> Result<Vec<String>, KbError> {
        {
            let mut cache = self.cache_attrs.lock().await;
            if let Some(cached) = cache.get(&interface_name.to_string()) {
                return Ok(cached.clone());
            }
        }

        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH (iface)-[:has_attribute]->(a) \
                     WHERE iface.name = $name \
                     RETURN a.name AS attr_name",
                )
                .param("name", interface_name),
            )
            .await?;

        let mut attrs = Vec::new();
        while let Ok(Some(row)) = result.next().await {
            if let Ok(name) = row.get::<String>("attr_name") {
                attrs.push(name);
            }
        }

        {
            let mut cache = self.cache_attrs.lock().await;
            cache.put(interface_name.to_string(), attrs.clone());
        }

        Ok(attrs)
    }

    // ---------------------------------------------------------------------------
    // Operator catalog queries
    // ---------------------------------------------------------------------------

    /// Get operators that perform a given task.
    pub async fn get_operators_for_task(
        &self,
        task_name: &str,
    ) -> Result<Vec<String>, KbError> {
        {
            let mut cache = self.cache_task_ops.lock().await;
            if let Some(cached) = cache.get(&task_name.to_string()) {
                return Ok(cached.clone());
            }
        }

        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH (op)-[:performs]->(task), (op)-[:is_an]->(:Node {name: 'Operator'}) \
                     WHERE task.name = $name \
                     RETURN op.name AS op_name",
                )
                .param("name", task_name),
            )
            .await?;

        let mut operators = Vec::new();
        while let Ok(Some(row)) = result.next().await {
            if let Ok(name) = row.get::<String>("op_name") {
                operators.push(name);
            }
        }

        {
            let mut cache = self.cache_task_ops.lock().await;
            cache.put(task_name.to_string(), operators.clone());
        }

        Ok(operators)
    }

    /// Get the family for an operator (e.g., "Ensemble", "Preprocessing").
    pub async fn get_operator_family(
        &self,
        operator_name: &str,
    ) -> Result<Option<String>, KbError> {
        {
            let mut cache = self.cache_family.lock().await;
            if let Some(cached) = cache.get(&operator_name.to_string()) {
                return Ok(cached.clone());
            }
        }

        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH (op)-[:implements]->(concept)-[:is_subclass_of]->(family) \
                     WHERE op.name = $name \
                     RETURN family.name AS family_name \
                     LIMIT 1",
                )
                .param("name", operator_name),
            )
            .await?;

        let family: Option<String> = match result.next().await? {
            Some(row) => row.get("family_name").ok(),
            None => None,
        };

        {
            let mut cache = self.cache_family.lock().await;
            cache.put(operator_name.to_string(), family.clone());
        }

        Ok(family)
    }

    /// Get all operators as a catalog.
    pub async fn get_all_operators(&self) -> Result<Vec<OperatorInfo>, KbError> {
        let mut result = self
            .graph
            .execute(query(
                "MATCH (op)-[:is_an]->(:Node {name: 'Operator'}) \
                 OPTIONAL MATCH (op)-[:is_a]->(iface)-[:is_an]->(:Node {name: 'Interface'}) \
                 OPTIONAL MATCH (op)-[:performs]->(task) \
                 OPTIONAL MATCH (op)-[:implements]->(concept)-[:is_subclass_of]->(family) \
                 RETURN op.name AS name, \
                        iface.name AS interface, \
                        COLLECT(DISTINCT task.name) AS tasks, \
                        family.name AS family",
            ))
            .await?;

        let mut operators = Vec::new();
        while let Ok(Some(row)) = result.next().await {
            let name: String = match row.get("name") {
                Ok(n) => n,
                Err(_) => continue,
            };

            operators.push(OperatorInfo {
                name,
                interface: row.get("interface").ok(),
                tasks: row.get::<Vec<String>>("tasks").unwrap_or_default(),
                family: row.get("family").ok(),
            });
        }

        Ok(operators)
    }

    // ---------------------------------------------------------------------------
    // Risk and mitigation queries
    // ---------------------------------------------------------------------------

    /// Get risks associated with an operator.
    pub async fn get_operator_risks(
        &self,
        operator_name: &str,
    ) -> Result<Vec<String>, KbError> {
        {
            let mut cache = self.cache_risks.lock().await;
            if let Some(cached) = cache.get(&operator_name.to_string()) {
                return Ok(cached.clone());
            }
        }

        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH (op)-[:checks_for]->(risk) \
                     WHERE op.name = $name \
                     RETURN risk.name AS risk_name",
                )
                .param("name", operator_name),
            )
            .await?;

        let mut risks = Vec::new();
        while let Ok(Some(row)) = result.next().await {
            if let Ok(name) = row.get::<String>("risk_name") {
                risks.push(name);
            }
        }

        {
            let mut cache = self.cache_risks.lock().await;
            cache.put(operator_name.to_string(), risks.clone());
        }

        Ok(risks)
    }

    /// Get mitigation specification for a named mitigation.
    pub async fn get_mitigation_spec(
        &self,
        mitigation_name: &str,
    ) -> Result<Option<MitigationSpec>, KbError> {
        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH (m)-[:is_a]->(iface)-[:is_an]->(:Node {name: 'Interface'}) \
                     WHERE m.name = $name \
                     OPTIONAL MATCH (iface)-[:has_input]->(inp) \
                     RETURN iface.name AS interface_name, \
                            COLLECT(DISTINCT inp.name) AS anchor_inputs",
                )
                .param("name", mitigation_name),
            )
            .await?;

        if let Ok(Some(row)) = result.next().await {
            let interface_name: String = match row.get("interface_name") {
                Ok(n) => n,
                Err(_) => return Ok(None),
            };

            Ok(Some(MitigationSpec {
                interface_name,
                anchor_inputs: row.get::<Vec<String>>("anchor_inputs").unwrap_or_default(),
            }))
        } else {
            Ok(None)
        }
    }

    /// Get the model family for an operator.
    pub async fn get_model_family(
        &self,
        operator_name: &str,
    ) -> Result<Option<String>, KbError> {
        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH (op)-[:belongs_to_family]->(f) \
                     WHERE op.name = $name \
                     RETURN f.name AS family_name \
                     LIMIT 1",
                )
                .param("name", operator_name),
            )
            .await?;

        match result.next().await? {
            Some(row) => Ok(row.get("family_name").ok()),
            None => Ok(None),
        }
    }

    /// Get families sensitive to a given risk.
    pub async fn get_sensitive_families_for_risk(
        &self,
        risk_name: &str,
    ) -> Result<Vec<String>, KbError> {
        let mut result = self
            .graph
            .execute(
                query(
                    "MATCH (risk)-[:sensitive_family]->(f) \
                     WHERE risk.name = $name \
                     RETURN f.name AS family_name",
                )
                .param("name", risk_name),
            )
            .await?;

        let mut families = Vec::new();
        while let Ok(Some(row)) = result.next().await {
            if let Ok(name) = row.get::<String>("family_name") {
                families.push(name);
            }
        }

        Ok(families)
    }

    /// Get all method names across all interfaces (for shortcut detection).
    pub async fn get_all_interface_methods(&self) -> Result<Vec<String>, KbError> {
        let mut result = self
            .graph
            .execute(query(
                "MATCH (iface)-[:is_an]->(:Node {name: 'Interface'}), \
                       (iface)-[:calls*]->(m:Method) \
                 WHERE m.name <> '__init__' \
                 RETURN DISTINCT m.name AS method_name",
            ))
            .await?;

        let mut methods = Vec::new();
        while let Ok(Some(row)) = result.next().await {
            if let Ok(name) = row.get::<String>("method_name") {
                methods.push(name);
            }
        }

        Ok(methods)
    }

    /// Get library package map (import_name → pip_package).
    pub async fn get_library_package_map(
        &self,
    ) -> Result<HashMap<String, String>, KbError> {
        let mut result = self
            .graph
            .execute(query(
                "MATCH (lib)-[:has_package]->(pkg) \
                 RETURN lib.name AS lib_name, pkg.name AS pkg_name",
            ))
            .await?;

        let mut map = HashMap::new();
        while let Ok(Some(row)) = result.next().await {
            if let (Ok(lib), Ok(pkg)) = (
                row.get::<String>("lib_name"),
                row.get::<String>("pkg_name"),
            ) {
                map.insert(lib, pkg);
            }
        }

        Ok(map)
    }

    // ---------------------------------------------------------------------------
    // Internal helpers
    // ---------------------------------------------------------------------------

    /// Query I/O ports for an interface by relationship type.
    async fn query_io_ports(
        &self,
        interface_name: &str,
        rel_type: &str,
    ) -> Result<Vec<IoSpec>, KbError> {
        let cypher = format!(
            "MATCH (iface)-[:{rel}]->(port) \
             WHERE iface.name = $name \
             OPTIONAL MATCH (port)-[:is_of_type]->(t) \
             OPTIONAL MATCH (t)-[:has_position]->(pos) \
             RETURN port.name AS port_name, t.name AS port_type, pos.name AS port_pos",
            rel = rel_type
        );

        let mut result = self
            .graph
            .execute(query(&cypher).param("name", interface_name))
            .await?;

        let mut ports = Vec::new();
        while let Ok(Some(row)) = result.next().await {
            if let Ok(name) = row.get::<String>("port_name") {
                ports.push(IoSpec {
                    name,
                    dtype: row
                        .get::<String>("port_type")
                        .unwrap_or_else(|_| "any".to_string()),
                    position: row
                        .get::<String>("port_pos")
                        .unwrap_or_else(|_| "0".to_string()),
                });
            }
        }

        ports.sort_by(|a, b| position_sort_key(&a.position).cmp(&position_sort_key(&b.position)));
        Ok(ports)
    }
}

/// Stable sort key for IoSpec.position — numeric positions come
/// first (in numeric order), kwarg names alphabetically. Mirrors
/// the same helper in ``kb/builder.rs``.
fn position_sort_key(p: &str) -> (u8, i64, String) {
    match p.parse::<i64>() {
        Ok(n) => (0, n, String::new()),
        Err(_) => (1, 0, p.to_string()),
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Extract a ParameterSpec from a query result row.
fn row_to_param_spec(row: &neo4rs::Row, method: Option<String>) -> Option<ParameterSpec> {
    let name: String = row.get("param_name").ok()?;

    Some(ParameterSpec {
        name,
        dtype: row
            .get::<String>("param_type")
            .unwrap_or_else(|_| "string".to_string()),
        default: row.get("param_default").ok(),
        low: row.get("param_low").ok(),
        high: row.get("param_high").ok(),
        choices: row
            .get::<String>("param_choices")
            .ok()
            .map(|s| s.split(',').map(|c| c.trim().to_string()).collect()),
        log_scale: row.get("param_log_scale").ok(),
        method,
    })
}

/// Merge a ParameterSpec into the map, keeping the richer definition.
fn merge_param(map: &mut HashMap<String, ParameterSpec>, spec: ParameterSpec) {
    if let Some(existing) = map.get(&spec.name) {
        if spec.richness() > existing.richness() {
            map.insert(spec.name.clone(), spec);
        }
    } else {
        map.insert(spec.name.clone(), spec);
    }
}

/// Parse a position string to its canonical form.
///
/// Positions are stored as strings in ``IoSpec.position``; numeric
/// positions look like ``"0"``/``"1"`` and kwarg-style positions
/// carry the kwarg name. Empty / ``"self"`` collapse to ``"0"``
/// (the chain-edge slot every method shortcut implicitly fills).
fn parse_position(s: &str) -> String {
    if s.is_empty() || s == "self" {
        return "0".to_string();
    }
    s.to_string()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_position() {
        assert_eq!(parse_position("0"), "0");
        assert_eq!(parse_position("1"), "1");
        assert_eq!(parse_position("self"), "0");
        assert_eq!(parse_position(""), "0");
        assert_eq!(parse_position("abc"), "abc");
        assert_eq!(parse_position("42"), "42");
        assert_eq!(parse_position("random_state"), "random_state");
    }

    #[test]
    fn test_parameter_spec_richness() {
        let bare = ParameterSpec {
            name: "p".into(),
            dtype: "int".into(),
            default: None,
            low: None,
            high: None,
            choices: None,
            log_scale: None,
            method: None,
        };
        assert_eq!(bare.richness(), 0);

        let rich = ParameterSpec {
            name: "p".into(),
            dtype: "int".into(),
            default: Some("10".into()),
            low: Some(1.0),
            high: Some(100.0),
            choices: None,
            log_scale: Some(false),
            method: None,
        };
        assert_eq!(rich.richness(), 4);
    }

    #[test]
    fn test_merge_param_keeps_richer() {
        let mut map = HashMap::new();

        let basic = ParameterSpec {
            name: "n_estimators".into(),
            dtype: "int".into(),
            default: Some("100".into()),
            low: None,
            high: None,
            choices: None,
            log_scale: None,
            method: None,
        };
        merge_param(&mut map, basic);

        let richer = ParameterSpec {
            name: "n_estimators".into(),
            dtype: "int".into(),
            default: Some("100".into()),
            low: Some(10.0),
            high: Some(500.0),
            choices: None,
            log_scale: Some(false),
            method: None,
        };
        merge_param(&mut map, richer.clone());

        assert_eq!(map.get("n_estimators").unwrap().richness(), richer.richness());
    }

    #[test]
    fn test_merge_param_does_not_replace_richer() {
        let mut map = HashMap::new();

        let richer = ParameterSpec {
            name: "lr".into(),
            dtype: "float".into(),
            default: Some("0.01".into()),
            low: Some(0.0001),
            high: Some(1.0),
            choices: None,
            log_scale: Some(true),
            method: None,
        };
        merge_param(&mut map, richer.clone());

        let basic = ParameterSpec {
            name: "lr".into(),
            dtype: "float".into(),
            default: Some("0.01".into()),
            low: None,
            high: None,
            choices: None,
            log_scale: None,
            method: None,
        };
        merge_param(&mut map, basic);

        // Should still be the richer one.
        assert_eq!(map.get("lr").unwrap().richness(), richer.richness());
    }

    #[test]
    fn test_parameter_spec_serialization() {
        let spec = ParameterSpec {
            name: "n_estimators".into(),
            dtype: "int".into(),
            default: Some("100".into()),
            low: Some(10.0),
            high: Some(500.0),
            choices: None,
            log_scale: Some(false),
            method: None,
        };

        let json = serde_json::to_string(&spec).unwrap();
        let deserialized: ParameterSpec = serde_json::from_str(&json).unwrap();
        assert_eq!(spec, deserialized);
    }

    #[test]
    fn test_io_spec_serialization() {
        let io = IoSpec {
            name: "X".into(),
            dtype: "DataFrame".into(),
            position: "1".into(),
        };
        let json = serde_json::to_string(&io).unwrap();
        let deserialized: IoSpec = serde_json::from_str(&json).unwrap();
        assert_eq!(io, deserialized);
    }

    #[test]
    fn test_io_spec_kwarg_position() {
        // Kwarg-style positions (the kwarg name) survive the
        // round-trip — the previous i32 type silently collapsed
        // these onto ``0`` and broke the SPA's handle rendering.
        let io = IoSpec {
            name: "random_state".into(),
            dtype: "any".into(),
            position: "random_state".into(),
        };
        let json = serde_json::to_string(&io).unwrap();
        let deserialized: IoSpec = serde_json::from_str(&json).unwrap();
        assert_eq!(io, deserialized);
        assert_eq!(deserialized.position, "random_state");
    }

    #[test]
    fn test_io_spec_legacy_numeric_position_deserialise() {
        // Older snapshot files stored ``position`` as a JSON number;
        // the new String type accepts both via the custom deserialiser.
        let json = r#"{"name":"X","dtype":"DataFrame","position":2}"#;
        let io: IoSpec = serde_json::from_str(json).unwrap();
        assert_eq!(io.position, "2");
    }

    #[test]
    fn test_operator_info_serialization() {
        let info = OperatorInfo {
            name: "sklearn.ensemble.RandomForestClassifier".into(),
            interface: Some("Sklearn Transformer".into()),
            tasks: vec!["classification".into()],
            family: Some("Ensemble".into()),
        };
        let json = serde_json::to_string(&info).unwrap();
        let deserialized: OperatorInfo = serde_json::from_str(&json).unwrap();
        assert_eq!(info, deserialized);
    }

    #[test]
    fn test_mitigation_spec_serialization() {
        let spec = MitigationSpec {
            interface_name: "Guardrail".into(),
            anchor_inputs: vec!["text".into(), "prompt".into()],
        };
        let json = serde_json::to_string(&spec).unwrap();
        let deserialized: MitigationSpec = serde_json::from_str(&json).unwrap();
        assert_eq!(spec, deserialized);
    }

    #[test]
    fn test_kb_error_display() {
        let err = KbError::Deserialize("missing field".to_string());
        assert!(err.to_string().contains("missing field"));
    }

    #[test]
    fn test_pathway_serialization() {
        let pathway = Pathway {
            name: "class_imbalance".into(),
            metric: "sklearn.metrics.balanced_accuracy_score".into(),
            direction: "below".into(),
            threshold: 0.5,
            families: vec!["Tree".into(), "Ensemble".into()],
            task: Some("classification".into()),
            preprocessing: None,
            replacement: None,
            description: Some("Class imbalance detected".into()),
            risk: Some("Class Imbalance".into()),
        };
        let json = serde_json::to_string(&pathway).unwrap();
        let deserialized: Pathway = serde_json::from_str(&json).unwrap();
        assert_eq!(pathway, deserialized);
    }
}
