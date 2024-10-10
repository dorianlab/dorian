rule bwa_map:
    input:
        fastq="samples/{sample}.fastq",
        idx=multiext("genome.fa", ".amb", ".ann", ".bwt", ".pac", ".sa")
    conda:
        "environment.yaml"
    output:
        "mapped_reads/{sample}.bam"
    params:
        idx=lambda w, input: os.path.splitext(input.idx[0])[0]
    shell:
        "bwa mem {params.idx} {input.fastq} | samtools view -Sb - > {output}"