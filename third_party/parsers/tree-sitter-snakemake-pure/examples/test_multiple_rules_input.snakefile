rule samtools_sort:
    input:
        "mapped_reads/{sample}.bam",
        "mapped_reads/{sample}.bam"
    output:
        "sorted_reads/{sample}.bam",
        "sorted_reads/{sample}.bam"
    conda:
        "environment.yaml"
    shell:
        "samtools sort -T sorted_reads/{wildcards.sample} "
rule samtools_sort:
    input:
        "mapped_reads/{sample}.bam",
        "sorted_reads/{sample}.bam"
    output:
        "sorted_reads/{sample}.bam",
        "sorted_reads/{sample}.bam"
    conda:
        "environment.yaml"
    shell:
        "samtools sort -T sorted_reads/{wildcards.sample} "