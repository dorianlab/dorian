fn main() -> Result<(), Box<dyn std::error::Error>> {
    let proto_dir = "../../proto";

    tonic_build::configure()
        .build_server(true)
        .build_client(false)
        .compile_protos(
            &[&format!("{proto_dir}/engine.proto")],
            &[proto_dir],
        )?;

    Ok(())
}
