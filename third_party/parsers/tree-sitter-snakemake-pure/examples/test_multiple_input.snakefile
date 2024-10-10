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
        "Line ni samtools sort -T sorted_reads/{wildcards.sample} "