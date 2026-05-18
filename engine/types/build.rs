fn main() -> Result<(), Box<dyn std::error::Error>> {
    let proto_dir = "../../proto";

    tonic_build::configure()
        .build_server(false)
        .build_client(false)
        .compile_protos(
            &[
                &format!("{proto_dir}/graph.proto"),
                &format!("{proto_dir}/execution.proto"),
                &format!("{proto_dir}/runtime.proto"),
                &format!("{proto_dir}/scaling.proto"),
                &format!("{proto_dir}/events.proto"),
            ],
            &[proto_dir],
        )?;

    Ok(())
}
