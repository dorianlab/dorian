rule samtools_index:
    input:
        "sorted_reads/{sample}.bam" 
    output:
        "sorted_reads/{sample}.bam.bai"
    conda:
        "environment.yaml"
    shell:
        "samtools index {input}"