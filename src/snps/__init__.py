""" `snps`

tools for reading, writing, merging, and remapping SNPs

"""

"""
BSD 3-Clause License

Copyright (c) 2019, Andrew Riha
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""

from itertools import groupby, count
import os
import re

import numpy as np
import pandas as pd
from pandas.api.types import CategoricalDtype

from snps.ensembl import EnsemblRestClient
from snps.resources import Resources
from snps.io import Reader, Writer
from snps.utils import save_df_as_csv, Parallelizer, clean_str

# set version string with Versioneer
from snps._version import get_versions

import logging

logger = logging.getLogger(__name__)

__version__ = get_versions()["version"]
del get_versions


class SNPs:
    def __init__(
        self,
        file="",
        only_detect_source=False,
        assign_par_snps=True,
        output_dir="output",
        resources_dir="resources",
        deduplicate=True,
        deduplicate_XY_chrom=True,
        parallelize=False,
        processes=os.cpu_count(),
        rsids=(),
    ):
        """ Object used to read and parse genotype / raw data files.

        Parameters
        ----------
        file : str or bytes
            path to file to load or bytes to load
        only_detect_source : bool
            only detect the source of the data
        assign_par_snps : bool
            assign PAR SNPs to the X and Y chromosomes
        output_dir : str
            path to output directory
        resources_dir : str
            name / path of resources directory
        deduplicate : bool
            deduplicate RSIDs and make SNPs available as `duplicate_snps`
        deduplicate_XY_chrom : bool
            deduplicate alleles in the non-PAR regions of X and Y for males; see `discrepant_XY_snps`
        parallelize : bool
            utilize multiprocessing to speedup calculations
        processes : int
            processes to launch if multiprocessing
        rsids : tuple, optional
            rsids to extract if loading a VCF file
        """
        self._file = file
        self._only_detect_source = only_detect_source
        self._snps = pd.DataFrame()
        self._duplicate_snps = pd.DataFrame()
        self._discrepant_XY_snps = pd.DataFrame()
        self._source = ""
        self._phased = False
        self._build = 0
        self._build_detected = False
        self._output_dir = output_dir
        self._resources = Resources(resources_dir=resources_dir)
        self._parallelizer = Parallelizer(parallelize=parallelize, processes=processes)

        if file:

            d = self._read_raw_data(file, only_detect_source, rsids)

            self._snps = d["snps"]
            self._source = d["source"]
            self._phased = d["phased"]

            if not self._snps.empty:
                self.sort_snps()

                if deduplicate:
                    self._deduplicate_rsids()

                self._build = self.detect_build()

                if not self._build:
                    self._build = 37  # assume Build 37 / GRCh37 if not detected
                else:
                    self._build_detected = True

                if deduplicate_XY_chrom:
                    if self.determine_sex() == "Male":
                        self._deduplicate_XY_chrom()

                if assign_par_snps:
                    self._assign_par_snps()

    def __repr__(self):
        return "SNPs({!r})".format(self._file[0:50])

    @property
    def source(self):
        """ Summary of the SNP data source for ``SNPs``.

        Returns
        -------
        str
        """
        return self._source

    @property
    def snps(self):
        """ Get a copy of SNPs.

        Returns
        -------
        pandas.DataFrame
        """
        return self._snps

    @property
    def duplicate_snps(self):
        """ Get any duplicate SNPs.

        A duplicate SNP has the same RSID as another SNP. The first occurrence
        of the RSID is not considered a duplicate SNP.

        Returns
        -------
        pandas.DataFrame
        """
        return self._duplicate_snps

    @property
    def discrepant_XY_snps(self):
        """ Get any discrepant XY SNPs.

        A discrepant XY SNP is a heterozygous SNP in the non-PAR region of the X
        or Y chromosome found during deduplication for a detected male genotype.

        Returns
        -------
        pandas.DataFrame
        """
        return self._discrepant_XY_snps

    @property
    def build(self):
        """ Get the build of ``SNPs``.

        Returns
        -------
        int
        """
        return self._build

    @property
    def build_detected(self):
        """ Get status indicating if build of ``SNPs`` was detected.

        Returns
        -------
        bool
        """
        return self._build_detected

    @property
    def assembly(self):
        """ Get the assembly of ``SNPs``.

        Returns
        -------
        str
        """
        return self.get_assembly()

    @property
    def snp_count(self):
        """ Count of SNPs.

        Returns
        -------
        int
        """
        return self.get_snp_count()

    @property
    def chromosomes(self):
        """ Chromosomes of ``SNPs``.

        Returns
        -------
        list
            list of str chromosomes (e.g., ['1', '2', '3', 'MT'], empty list if no chromosomes
        """
        return self.get_chromosomes()

    @property
    def chromosomes_summary(self):
        """ Summary of the chromosomes of ``SNPs``.

        Returns
        -------
        str
            human-readable listing of chromosomes (e.g., '1-3, MT'), empty str if no chromosomes
        """
        return self.get_chromosomes_summary()

    @property
    def sex(self):
        """ Sex derived from ``SNPs``.

        Returns
        -------
        str
            'Male' or 'Female' if detected, else empty str
        """
        sex = self.determine_sex(chrom="X")
        if not sex:
            sex = self.determine_sex(chrom="Y")
        return sex

    @property
    def unannotated_vcf(self):
        """ Indicates if VCF file is unannotated.

        Returns
        -------
        bool
        """
        if self.snp_count == 0 and self.source == "vcf":
            return True

        return False

    @property
    def phased(self):
        """ Indicates if genotype is phased.

        Returns
        -------
        bool
        """
        return self._phased

    def heterozygous_snps(self, chrom=""):
        """ Get heterozygous SNPs.

        Parameters
        ----------
        chrom : str, optional
            chromosome (e.g., "1", "X", "MT")

        Returns
        -------
        pandas.DataFrame
        """
        if chrom:
            return self._snps.loc[
                (self._snps.chrom == chrom)
                & (self._snps.genotype.notnull())
                & (self._snps.genotype.str.len() == 2)
                & (self._snps.genotype.str[0] != self._snps.genotype.str[1])
            ]
        else:
            return self._snps.loc[
                (self._snps.genotype.notnull())
                & (self._snps.genotype.str.len() == 2)
                & (self._snps.genotype.str[0] != self._snps.genotype.str[1])
            ]

    def not_null_snps(self, chrom=""):
        """ Get not null SNPs.

        Parameters
        ----------
        chrom : str, optional
            chromosome (e.g., "1", "X", "MT")

        Returns
        -------
        pandas.DataFrame
        """

        if chrom:
            return self._snps.loc[
                (self._snps.chrom == chrom) & (self._snps.genotype.notnull())
            ]
        else:
            return self._snps.loc[self._snps.genotype.notnull()]

    def get_summary(self):
        """ Get summary of ``SNPs``.

        Returns
        -------
        dict
            summary info if ``SNPs`` is valid, else {}
        """
        if not self.is_valid():
            return {}
        else:
            return {
                "source": self.source,
                "assembly": self.assembly,
                "build": self.build,
                "build_detected": self.build_detected,
                "snp_count": self.snp_count,
                "chromosomes": self.chromosomes_summary,
                "sex": self.sex,
            }

    def is_valid(self):
        """ Determine if ``SNPs`` is valid.

        ``SNPs`` is valid when the input file has been successfully parsed.

        Returns
        -------
        bool
            True if ``SNPs`` is valid
        """
        if self._snps.empty:
            return False
        else:
            return True

    def save_snps(self, filename="", vcf=False, atomic=True, **kwargs):
        """ Save SNPs to file.

        Parameters
        ----------
        filename : str or buffer
            filename for file to save or buffer to write to
        vcf : bool
            flag to save file as VCF
        atomic : bool
            atomically write output to a file on local filesystem
        **kwargs
            additional parameters to `pandas.DataFrame.to_csv`

        Returns
        -------
        str
            path to file in output directory if SNPs were saved, else empty str
        """
        return Writer.write_file(
            snps=self, filename=filename, vcf=vcf, atomic=atomic, **kwargs
        )

    def _read_raw_data(self, file, only_detect_source, rsids):
        return Reader.read_file(file, only_detect_source, self._resources, rsids)

    def _assign_par_snps(self):
        """ Assign PAR SNPs to the X or Y chromosome using SNP position.

        References
        -----
        1. National Center for Biotechnology Information, Variation Services, RefSNP,
           https://api.ncbi.nlm.nih.gov/variation/v0/
        2. Yates et. al. (doi:10.1093/bioinformatics/btu613),
           `<http://europepmc.org/search/?query=DOI:10.1093/bioinformatics/btu613>`_
        3. Zerbino et. al. (doi.org/10.1093/nar/gkx1098), https://doi.org/10.1093/nar/gkx1098
        4. Sherry ST, Ward MH, Kholodov M, Baker J, Phan L, Smigielski EM, Sirotkin K.
           dbSNP: the NCBI database of genetic variation. Nucleic Acids Res. 2001 Jan 1;
           29(1):308-11.
        5. Database of Single Nucleotide Polymorphisms (dbSNP). Bethesda (MD): National Center
           for Biotechnology Information, National Library of Medicine. dbSNP accession:
           rs28736870, rs113313554, and rs758419898 (dbSNP Build ID: 151). Available from:
           http://www.ncbi.nlm.nih.gov/SNP/
        """
        rest_client = EnsemblRestClient(
            server="https://api.ncbi.nlm.nih.gov", reqs_per_sec=1
        )
        for rsid in self._snps.loc[self._snps["chrom"] == "PAR"].index.values:
            if "rs" in rsid:
                id = rsid.split("rs")[1]
                response = rest_client.perform_rest_action("/variation/v0/refsnp/" + id)

                if response is not None:
                    for item in response["primary_snapshot_data"][
                        "placements_with_allele"
                    ]:
                        if "NC_000023" in item["seq_id"]:
                            assigned = self._assign_snp(rsid, item["alleles"], "X")
                        elif "NC_000024" in item["seq_id"]:
                            assigned = self._assign_snp(rsid, item["alleles"], "Y")
                        else:
                            assigned = False

                        if assigned:
                            if not self._build_detected:
                                self._build = self._extract_build(item)
                                self._build_detected = True
                            break

    def _assign_snp(self, rsid, alleles, chrom):
        # only assign SNP if positions match (i.e., same build)
        for allele in alleles:
            allele_pos = allele["allele"]["spdi"]["position"]
            # ref SNP positions seem to be 0-based...
            if allele_pos == self._snps.loc[rsid].pos - 1:
                self._snps.loc[rsid, "chrom"] = chrom
                return True
        return False

    def _extract_build(self, item):
        assembly_name = item["placement_annot"]["seq_id_traits_by_assembly"][0][
            "assembly_name"
        ]
        assembly_name = assembly_name.split(".")[0]
        return int(assembly_name[-2:])

    def detect_build(self):
        """ Detect build of SNPs.

        Use the coordinates of common SNPs to identify the build / assembly of a genotype file
        that is being loaded.

        Notes
        -----
        rs3094315 : plus strand in 36, 37, and 38
        rs11928389 : plus strand in 36, minus strand in 37 and 38
        rs2500347 : plus strand in 36 and 37, minus strand in 38
        rs964481 : plus strand in 36, 37, and 38
        rs2341354 : plus strand in 36, 37, and 38

        Returns
        -------
        int
            detected build of SNPs, else 0

        References
        ----------
        1. Yates et. al. (doi:10.1093/bioinformatics/btu613),
           `<http://europepmc.org/search/?query=DOI:10.1093/bioinformatics/btu613>`_
        2. Zerbino et. al. (doi.org/10.1093/nar/gkx1098), https://doi.org/10.1093/nar/gkx1098
        3. Sherry ST, Ward MH, Kholodov M, Baker J, Phan L, Smigielski EM, Sirotkin K.
           dbSNP: the NCBI database of genetic variation. Nucleic Acids Res. 2001
           Jan 1;29(1):308-11.
        4. Database of Single Nucleotide Polymorphisms (dbSNP). Bethesda (MD): National Center
           for Biotechnology Information, National Library of Medicine. dbSNP accession: rs3094315,
           rs11928389, rs2500347, rs964481, and rs2341354 (dbSNP Build ID: 151). Available from:
           http://www.ncbi.nlm.nih.gov/SNP/
        """

        def lookup_build_with_snp_pos(pos, s):
            try:
                return s.loc[s == pos].index[0]
            except:
                return 0

        build = 0

        rsids = ["rs3094315", "rs11928389", "rs2500347", "rs964481", "rs2341354"]
        df = pd.DataFrame(
            {
                36: [742429, 50908372, 143649677, 27566744, 908436],
                37: [752566, 50927009, 144938320, 27656823, 918573],
                38: [817186, 50889578, 148946169, 27638706, 983193],
            },
            index=rsids,
        )

        for rsid in rsids:
            if rsid in self._snps.index:
                build = lookup_build_with_snp_pos(
                    self._snps.loc[rsid].pos, df.loc[rsid]
                )

            if build:
                break

        return build

    def get_assembly(self):
        """ Get the assembly of a build.

        Returns
        -------
        str
        """

        if self._build == 37:
            return "GRCh37"
        elif self._build == 36:
            return "NCBI36"
        elif self._build == 38:
            return "GRCh38"
        else:
            return ""

    def get_snp_count(self, chrom=""):
        """ Count of SNPs.

        Parameters
        ----------
        chrom : str, optional
            chromosome (e.g., "1", "X", "MT")

        Returns
        -------
        int
        """
        if chrom:
            return len(self._snps.loc[(self._snps.chrom == chrom)])
        else:
            return len(self._snps)

    def get_chromosomes(self):
        """ Get the chromosomes of SNPs.

        Returns
        -------
        list
            list of str chromosomes (e.g., ['1', '2', '3', 'MT'], empty list if no chromosomes
        """

        if not self._snps.empty:
            return list(pd.unique(self._snps["chrom"]))
        else:
            return []

    def get_chromosomes_summary(self):
        """ Summary of the chromosomes of SNPs.

        Returns
        -------
        str
            human-readable listing of chromosomes (e.g., '1-3, MT'), empty str if no chromosomes
        """

        if not self._snps.empty:
            chroms = list(pd.unique(self._snps["chrom"]))

            int_chroms = [int(chrom) for chrom in chroms if chrom.isdigit()]
            str_chroms = [chrom for chrom in chroms if not chrom.isdigit()]

            # https://codereview.stackexchange.com/a/5202
            def as_range(iterable):
                l = list(iterable)
                if len(l) > 1:
                    return "{0}-{1}".format(l[0], l[-1])
                else:
                    return "{0}".format(l[0])

            # create str representations
            int_chroms = ", ".join(
                as_range(g)
                for _, g in groupby(int_chroms, key=lambda n, c=count(): n - next(c))
            )
            str_chroms = ", ".join(str_chroms)

            if int_chroms != "" and str_chroms != "":
                int_chroms += ", "

            return int_chroms + str_chroms
        else:
            return ""

    def determine_sex(
        self,
        heterozygous_x_snps_threshold=0.03,
        y_snps_not_null_threshold=0.3,
        chrom="X",
    ):
        """ Determine sex from SNPs using thresholds.

        Parameters
        ----------
        heterozygous_x_snps_threshold : float
            percentage heterozygous X SNPs; above this threshold, Female is determined
        y_snps_not_null_threshold : float
            percentage Y SNPs that are not null; above this threshold, Male is determined
        chrom : {"X", "Y"}
            use X or Y chromosome SNPs to determine sex

        Returns
        -------
        str
            'Male' or 'Female' if detected, else empty str
        """
        if not self._snps.empty:
            if chrom == "X":
                return self._determine_sex_X(heterozygous_x_snps_threshold)
            elif chrom == "Y":
                return self._determine_sex_Y(y_snps_not_null_threshold)
        return ""

    def _determine_sex_X(self, threshold):
        x_snps = self.get_snp_count("X")

        if x_snps > 0:
            if len(self.heterozygous_snps("X")) / x_snps > threshold:
                return "Female"
            else:
                return "Male"
        else:
            return ""

    def _determine_sex_Y(self, threshold):
        y_snps = self.get_snp_count("Y")

        if y_snps > 0:
            if len(self.not_null_snps("Y")) / y_snps > threshold:
                return "Male"
            else:
                return "Female"
        else:
            return ""

    def _get_non_par_start_stop(self, chrom):
        # get non-PAR start / stop positions for chrom
        pr = self.get_par_regions(self.build)
        np_start = pr.loc[(pr.chrom == chrom) & (pr.region == "PAR1")].stop.values[0]
        np_stop = pr.loc[(pr.chrom == chrom) & (pr.region == "PAR2")].start.values[0]
        return np_start, np_stop

    def _get_non_par_snps(self, chrom, heterozygous=True):
        np_start, np_stop = self._get_non_par_start_stop(chrom)

        if heterozygous:
            # get heterozygous SNPs in the non-PAR region (i.e., discrepant XY SNPs)
            return self._snps.loc[
                (self._snps.chrom == chrom)
                & (self._snps.genotype.notnull())
                & (self._snps.genotype.str.len() == 2)
                & (self._snps.genotype.str[0] != self._snps.genotype.str[1])
                & (self._snps.pos > np_start)
                & (self._snps.pos < np_stop)
            ].index
        else:
            # get homozygous SNPs in the non-PAR region
            return self._snps.loc[
                (self._snps.chrom == chrom)
                & (self._snps.genotype.notnull())
                & (self._snps.genotype.str.len() == 2)
                & (self._snps.genotype.str[0] == self._snps.genotype.str[1])
                & (self._snps.pos > np_start)
                & (self._snps.pos < np_stop)
            ].index

    def _deduplicate_rsids(self):
        # Keep first duplicate rsid.
        duplicate_rsids = self._snps.index.duplicated(keep="first")
        # save duplicate SNPs
        self._duplicate_snps = self._duplicate_snps.append(
            self._snps.loc[duplicate_rsids]
        )
        # deduplicate
        self._snps = self._snps.loc[~duplicate_rsids]

    def _deduplicate_chrom(self, chrom):
        """ Deduplicate a chromosome in the non-PAR region. """

        discrepant_XY_snps = self._get_non_par_snps(chrom)

        # save discrepant XY SNPs
        self._discrepant_XY_snps = self._discrepant_XY_snps.append(
            self._snps.loc[discrepant_XY_snps]
        )

        # drop discrepant XY SNPs since it's ambiguous for which allele to deduplicate
        self._snps.drop(discrepant_XY_snps, inplace=True)

        # get remaining non-PAR SNPs with two alleles
        non_par_snps = self._get_non_par_snps(chrom, heterozygous=False)

        # remove duplicate allele
        self._snps.loc[non_par_snps, "genotype"] = self._snps.loc[
            non_par_snps, "genotype"
        ].apply(lambda x: x[0])

    def _deduplicate_XY_chrom(self):
        """ Fix chromosome issue where some data providers duplicate male X and Y chromosomes"""
        self._deduplicate_chrom("X")
        self._deduplicate_chrom("Y")

    @staticmethod
    def get_par_regions(build):
        """ Get PAR regions for the X and Y chromosomes.

        Parameters
        ----------
        build : int
            build of SNPs

        Returns
        -------
        pandas.DataFrame
            PAR regions for the given build

        References
        ----------
        1. Genome Reference Consortium, https://www.ncbi.nlm.nih.gov/grc/human
        2. Yates et. al. (doi:10.1093/bioinformatics/btu613),
           `<http://europepmc.org/search/?query=DOI:10.1093/bioinformatics/btu613>`_
        3. Zerbino et. al. (doi.org/10.1093/nar/gkx1098), https://doi.org/10.1093/nar/gkx1098
        """
        if build == 37:
            return pd.DataFrame(
                {
                    "region": ["PAR1", "PAR2", "PAR1", "PAR2"],
                    "chrom": ["X", "X", "Y", "Y"],
                    "start": [60001, 154931044, 10001, 59034050],
                    "stop": [2699520, 155260560, 2649520, 59363566],
                },
                columns=["region", "chrom", "start", "stop"],
            )
        elif build == 38:
            return pd.DataFrame(
                {
                    "region": ["PAR1", "PAR2", "PAR1", "PAR2"],
                    "chrom": ["X", "X", "Y", "Y"],
                    "start": [10001, 155701383, 10001, 56887903],
                    "stop": [2781479, 156030895, 2781479, 57217415],
                },
                columns=["region", "chrom", "start", "stop"],
            )
        elif build == 36:
            return pd.DataFrame(
                {
                    "region": ["PAR1", "PAR2", "PAR1", "PAR2"],
                    "chrom": ["X", "X", "Y", "Y"],
                    "start": [1, 154584238, 1, 57443438],
                    "stop": [2709520, 154913754, 2709520, 57772954],
                },
                columns=["region", "chrom", "start", "stop"],
            )
        else:
            return pd.DataFrame()

    def sort_snps(self):
        """ Sort SNPs based on ordered chromosome list and position. """

        sorted_list = sorted(self._snps["chrom"].unique(), key=self._natural_sort_key)

        # move PAR and MT to the end of the dataframe
        if "PAR" in sorted_list:
            sorted_list.remove("PAR")
            sorted_list.append("PAR")

        if "MT" in sorted_list:
            sorted_list.remove("MT")
            sorted_list.append("MT")

        # convert chrom column to category for sorting
        # https://stackoverflow.com/a/26707444
        self._snps["chrom"] = self._snps["chrom"].astype(
            CategoricalDtype(categories=sorted_list, ordered=True)
        )

        # sort based on ordered chromosome list and position
        snps = self._snps.sort_values(["chrom", "pos"])

        # convert chromosome back to object
        snps["chrom"] = snps["chrom"].astype(object)

        self._snps = snps

    def remap_snps(self, target_assembly, complement_bases=True):
        """ Remap SNP coordinates from one assembly to another.

        This method uses the assembly map endpoint of the Ensembl REST API service (via
        ``Resources``'s ``EnsemblRestClient``) to convert SNP coordinates / positions from one
        assembly to another. After remapping, the coordinates / positions for the
        SNPs will be that of the target assembly.

        If the SNPs are already mapped relative to the target assembly, remapping will not be
        performed.

        Parameters
        ----------
        target_assembly : {'NCBI36', 'GRCh37', 'GRCh38', 36, 37, 38}
            assembly to remap to
        complement_bases : bool
            complement bases when remapping SNPs to the minus strand

        Returns
        -------
        chromosomes_remapped : list of str
            chromosomes remapped
        chromosomes_not_remapped : list of str
            chromosomes not remapped

        Notes
        -----
        An assembly is also know as a "build." For example:

        Assembly NCBI36 = Build 36
        Assembly GRCh37 = Build 37
        Assembly GRCh38 = Build 38

        See https://www.ncbi.nlm.nih.gov/assembly for more information about assemblies and
        remapping.

        References
        ----------
        1. Ensembl, Assembly Map Endpoint,
           http://rest.ensembl.org/documentation/info/assembly_map
        """
        chromosomes_remapped = []
        chromosomes_not_remapped = []

        snps = self.snps

        if snps.empty:
            logger.warning("No SNPs to remap")
            return chromosomes_remapped, chromosomes_not_remapped
        else:
            chromosomes = snps["chrom"].unique()
            chromosomes_not_remapped = list(chromosomes)

        valid_assemblies = ["NCBI36", "GRCh37", "GRCh38", 36, 37, 38]

        if target_assembly not in valid_assemblies:
            logger.warning("Invalid target assembly")
            return chromosomes_remapped, chromosomes_not_remapped

        if isinstance(target_assembly, int):
            if target_assembly == 36:
                target_assembly = "NCBI36"
            else:
                target_assembly = "GRCh" + str(target_assembly)

        if self.build == 36:
            source_assembly = "NCBI36"
        else:
            source_assembly = "GRCh" + str(self.build)

        if source_assembly == target_assembly:
            return chromosomes_remapped, chromosomes_not_remapped

        assembly_mapping_data = self._resources.get_assembly_mapping_data(
            source_assembly, target_assembly
        )

        if not assembly_mapping_data:
            return chromosomes_remapped, chromosomes_not_remapped

        tasks = []

        for chrom in chromosomes:
            if chrom in assembly_mapping_data:
                chromosomes_remapped.append(chrom)
                chromosomes_not_remapped.remove(chrom)
                mappings = assembly_mapping_data[chrom]
                tasks.append(
                    {
                        "snps": snps.loc[snps["chrom"] == chrom],
                        "mappings": mappings,
                        "complement_bases": complement_bases,
                    }
                )
            else:
                logger.warning(
                    "Chromosome {} not remapped; "
                    "removing chromosome from SNPs for consistency".format(chrom)
                )
                snps = snps.drop(snps.loc[snps["chrom"] == chrom].index)

        # remap SNPs
        remapped_snps = self._parallelizer(self._remapper, tasks)
        remapped_snps = pd.concat(remapped_snps)

        # update SNP positions and genotypes
        snps.loc[remapped_snps.index, "pos"] = remapped_snps["pos"]
        snps.loc[remapped_snps.index, "genotype"] = remapped_snps["genotype"]

        self._snps = snps
        self.sort_snps()
        self._build = int(target_assembly[-2:])

        return chromosomes_remapped, chromosomes_not_remapped

    def _remapper(self, task):
        """ Remap SNPs for a chromosome.

        Parameters
        ----------
        task : dict
            dict with `snps` to remap per `mappings`, optionally `complement_bases`

        Returns
        -------
        pandas.DataFrame
            remapped SNPs
        """
        temp = task["snps"].copy()
        mappings = task["mappings"]
        complement_bases = task["complement_bases"]

        temp["remapped"] = False

        pos_start = int(temp["pos"].describe()["min"])
        pos_end = int(temp["pos"].describe()["max"])

        for mapping in mappings["mappings"]:
            # skip if mapping is outside of range of SNP positions
            if (
                mapping["original"]["end"] <= pos_start
                or mapping["original"]["start"] >= pos_end
            ):
                continue

            orig_range_len = mapping["original"]["end"] - mapping["original"]["start"]
            mapped_range_len = mapping["mapped"]["end"] - mapping["mapped"]["start"]

            orig_region = mapping["original"]["seq_region_name"]
            mapped_region = mapping["mapped"]["seq_region_name"]

            if orig_region != mapped_region:
                logger.warning("discrepant chroms")
                continue

            if orig_range_len != mapped_range_len:
                logger.warning(
                    "discrepant coords"
                )  # observed when mapping NCBI36 -> GRCh38
                continue

            # find the SNPs that are being remapped for this mapping
            snp_indices = temp.loc[
                ~temp["remapped"]
                & (temp["pos"] >= mapping["original"]["start"])
                & (temp["pos"] <= mapping["original"]["end"])
            ].index

            if len(snp_indices) > 0:
                # remap the SNPs
                if mapping["mapped"]["strand"] == -1:
                    # flip and (optionally) complement since we're mapping to minus strand
                    diff_from_start = (
                        temp.loc[snp_indices, "pos"] - mapping["original"]["start"]
                    )
                    temp.loc[snp_indices, "pos"] = (
                        mapping["mapped"]["end"] - diff_from_start
                    )

                    if complement_bases:
                        temp.loc[snp_indices, "genotype"] = temp.loc[
                            snp_indices, "genotype"
                        ].apply(self._complement_bases)
                else:
                    # mapping is on same (plus) strand, so just remap based on offset
                    offset = mapping["mapped"]["start"] - mapping["original"]["start"]
                    temp.loc[snp_indices, "pos"] = temp["pos"] + offset

                # mark these SNPs as remapped
                temp.loc[snp_indices, "remapped"] = True

        return temp

    def _complement_bases(self, genotype):
        if pd.isnull(genotype):
            return np.nan

        complement = ""

        for base in list(genotype):
            if base == "A":
                complement += "T"
            elif base == "G":
                complement += "C"
            elif base == "C":
                complement += "G"
            elif base == "T":
                complement += "A"
            else:
                complement += base

        return complement

    # https://stackoverflow.com/a/16090640
    @staticmethod
    def _natural_sort_key(s, natural_sort_re=re.compile("([0-9]+)")):
        return [
            int(text) if text.isdigit() else text.lower()
            for text in re.split(natural_sort_re, s)
        ]


class SNPsCollection(SNPs):
    def __init__(self, raw_data=None, output_dir="output", name="", **kwargs):
        """

        Parameters
        ----------
        raw_data : list or str
            path(s) to file(s) with raw genotype data
        output_dir : str
            path to output directory
        name : str
            name for this ``SNPsCollection``
        """
        super().__init__(file="", output_dir=output_dir, **kwargs)

        self._source = []
        self._discrepant_positions_file_count = 0
        self._discrepant_genotypes_file_count = 0
        self._discrepant_positions = pd.DataFrame()
        self._discrepant_genotypes = pd.DataFrame()
        self._name = name

        if raw_data is not None:
            self.load_snps(raw_data)

    def __repr__(self):
        return "SNPsCollection(name={!r})".format(self._name)

    @property
    def source(self):
        """ Summary of the SNP data source for ``SNPs``.

        Returns
        -------
        str
        """
        return ", ".join(self._source)

    @property
    def discrepant_positions(self):
        """ SNPs with discrepant positions discovered while loading SNPs.

        Returns
        -------
        pandas.DataFrame
        """
        return self._discrepant_positions

    @property
    def discrepant_genotypes(self):
        """ SNPs with discrepant genotypes discovered while loading SNPs.

        Returns
        -------
        pandas.DataFrame
        """
        return self._discrepant_genotypes

    @property
    def discrepant_snps(self):
        """ SNPs with discrepant positions and / or genotypes discovered while loading SNPs.

        Returns
        -------
        pandas.DataFrame
        """
        df = self._discrepant_positions.append(self._discrepant_genotypes)
        if len(df) > 1:
            df = df.drop_duplicates()
        return df

    def load_snps(
        self,
        raw_data,
        discrepant_snp_positions_threshold=100,
        discrepant_genotypes_threshold=500,
        save_output=False,
    ):
        """ Load raw genotype data.

        Parameters
        ----------
        raw_data : list or str
            path(s) to file(s) with raw genotype data
        discrepant_snp_positions_threshold : int
            threshold for discrepant SNP positions between existing data and data to be loaded,
            a large value could indicate mismatched genome assemblies
        discrepant_genotypes_threshold : int
            threshold for discrepant genotype data between existing data and data to be loaded,
            a large value could indicated mismatched individuals
        save_output : bool
            specifies whether to save discrepant SNP output to CSV files in the output directory
        """
        if type(raw_data) is list:
            for file in raw_data:
                self._load_snps_helper(
                    file,
                    discrepant_snp_positions_threshold,
                    discrepant_genotypes_threshold,
                    save_output,
                )
        elif type(raw_data) is str:
            self._load_snps_helper(
                raw_data,
                discrepant_snp_positions_threshold,
                discrepant_genotypes_threshold,
                save_output,
            )
        else:
            raise TypeError("invalid filetype")

    def _load_snps_helper(
        self,
        file,
        discrepant_snp_positions_threshold,
        discrepant_genotypes_threshold,
        save_output,
    ):
        logger.info("Loading {}".format(os.path.relpath(file)))
        discrepant_positions, discrepant_genotypes = self._add_snps(
            SNPs(file),
            discrepant_snp_positions_threshold,
            discrepant_genotypes_threshold,
            save_output,
        )

        self._discrepant_positions = self._discrepant_positions.append(
            discrepant_positions, sort=True
        )
        self._discrepant_genotypes = self._discrepant_genotypes.append(
            discrepant_genotypes, sort=True
        )

    def save_snps(self, filename="", vcf=False, atomic=True, **kwargs):
        """ Save SNPs to file.

        Parameters
        ----------
        filename : str
            filename for file to save
        vcf : bool
            flag to save file as VCF
        atomic : bool
            atomically write output to a file on local filesystem
        **kwargs
            additional parameters to `pandas.DataFrame.to_csv`

        Returns
        -------
        str
            path to file in output directory if SNPs were saved, else empty str
        """
        if not self._name:
            prefix = ""
        else:
            prefix = "{}_".format(clean_str(self._name))

        if not filename:
            if vcf:
                ext = ".vcf"
            else:
                ext = ".csv"

            filename = "{}{}{}".format(prefix, self.assembly, ext)
        return super().save_snps(filename=filename, vcf=vcf, atomic=atomic, **kwargs)

    def save_discrepant_positions(self, filename=""):
        """ Save SNPs with discrepant positions to file.

        Parameters
        ----------
        filename : str
            filename for file to save

        Returns
        -------
        str
            path to file in output directory if SNPs were saved, else empty str
        """
        return self._save_discrepant_snps_file(
            self.discrepant_positions, "discrepant_positions", filename
        )

    def save_discrepant_genotypes(self, filename=""):
        """ Save SNPs with discrepant genotypes to file.

        Parameters
        ----------
        filename : str
            filename for file to save

        Returns
        -------
        str
            path to file in output directory if SNPs were saved, else empty str
        """
        return self._save_discrepant_snps_file(
            self.discrepant_genotypes, "discrepant_genotypes", filename
        )

    def save_discrepant_snps(self, filename=""):
        """ Save SNPs with discrepant positions and / or genotypes to file.

        Parameters
        ----------
        filename : str
            filename for file to save

        Returns
        -------
        str
            path to file in output directory if SNPs were saved, else empty str
        """
        return self._save_discrepant_snps_file(
            self.discrepant_snps, "discrepant_snps", filename
        )

    def _save_discrepant_snps_file(self, df, discrepant_snps_type, filename):
        if not filename:
            if not self._name:
                filename = "{}.csv".format(discrepant_snps_type)
            else:
                filename = "{}_{}.csv".format(
                    clean_str(self._name), discrepant_snps_type
                )

        return save_df_as_csv(
            df,
            self._output_dir,
            filename,
            comment="# Source(s): {}\n".format(self.source),
        )

    def _add_snps(
        self,
        snps,
        discrepant_snp_positions_threshold,
        discrepant_genotypes_threshold,
        save_output,
    ):
        """ Add SNPs to this ``SNPsCollection``.

        Parameters
        ----------
        snps : SNPs
            SNPs to add
        discrepant_snp_positions_threshold : int
            see above
        discrepant_genotypes_threshold : int
            see above
        save_output
            see above

        Returns
        -------
        discrepant_positions : pandas.DataFrame
        discrepant_genotypes : pandas.DataFrame
        """
        discrepant_positions = pd.DataFrame()
        discrepant_genotypes = pd.DataFrame()

        if snps._snps.empty:
            return discrepant_positions, discrepant_genotypes

        build = snps._build
        source = [s.strip() for s in snps._source.split(",")]

        if not snps._build_detected:
            logger.warning("build not detected, assuming build {}".format(snps._build))

        if not self._build:
            self._build = build
        elif self._build != build:
            logger.warning(
                "build / assembly mismatch between current build of SNPs and SNPs being loaded"
            )

        # ensure there are always two X alleles
        snps = self._double_single_alleles(snps._snps, "X")

        if self._snps.empty:
            self._source.extend(source)
            self._snps = snps
        else:
            common_snps = self._snps.join(snps, how="inner", rsuffix="_added")

            discrepant_positions = common_snps.loc[
                (common_snps["chrom"] != common_snps["chrom_added"])
                | (common_snps["pos"] != common_snps["pos_added"])
            ]

            if not self._name:
                prefix = ""
            else:
                prefix = "{}_".format(clean_str(self._name))

            if 0 < len(discrepant_positions) < discrepant_snp_positions_threshold:
                logger.warning(
                    "{} SNP positions were discrepant; keeping original positions".format(
                        str(len(discrepant_positions))
                    )
                )

                if save_output:
                    self._discrepant_positions_file_count += 1
                    save_df_as_csv(
                        discrepant_positions,
                        self._output_dir,
                        "{}discrepant_positions_{}{}".format(
                            prefix, str(self._discrepant_positions_file_count), ".csv"
                        ),
                    )
            elif len(discrepant_positions) >= discrepant_snp_positions_threshold:
                logger.warning(
                    "too many SNPs differ in position; ensure same genome build is being used"
                )
                return discrepant_positions, discrepant_genotypes

            # remove null genotypes
            common_snps = common_snps.loc[
                ~common_snps["genotype"].isnull()
                & ~common_snps["genotype_added"].isnull()
            ]

            # discrepant genotypes are where alleles are not equivalent (i.e., alleles are not the
            # same and not swapped)
            discrepant_genotypes = common_snps.loc[
                (
                    (common_snps["genotype"].str.len() == 1)
                    & (common_snps["genotype_added"].str.len() == 1)
                    & ~(
                        common_snps["genotype"].str[0]
                        == common_snps["genotype_added"].str[0]
                    )
                )
                | (
                    (common_snps["genotype"].str.len() == 2)
                    & (common_snps["genotype_added"].str.len() == 2)
                    & ~(
                        (
                            common_snps["genotype"].str[0]
                            == common_snps["genotype_added"].str[0]
                        )
                        & (
                            common_snps["genotype"].str[1]
                            == common_snps["genotype_added"].str[1]
                        )
                    )
                    & ~(
                        (
                            common_snps["genotype"].str[0]
                            == common_snps["genotype_added"].str[1]
                        )
                        & (
                            common_snps["genotype"].str[1]
                            == common_snps["genotype_added"].str[0]
                        )
                    )
                )
            ]

            if 0 < len(discrepant_genotypes) < discrepant_genotypes_threshold:
                logger.warning(
                    "{} SNP genotypes were discrepant; marking those as null".format(
                        str(len(discrepant_genotypes))
                    )
                )

                if save_output:
                    self._discrepant_genotypes_file_count += 1
                    save_df_as_csv(
                        discrepant_genotypes,
                        self._output_dir,
                        "{}discrepant_genotypes_{}{}".format(
                            prefix, str(self._discrepant_genotypes_file_count), ".csv"
                        ),
                    )
            elif len(discrepant_genotypes) >= discrepant_genotypes_threshold:
                logger.warning(
                    "too many SNPs differ in their genotype; ensure file is for same "
                    "individual"
                )
                return discrepant_positions, discrepant_genotypes

            # add new SNPs
            self._source.extend(source)
            self._snps = self._snps.combine_first(snps)
            self._snps.loc[discrepant_genotypes.index, "genotype"] = np.nan

            # combine_first converts position to float64, so convert it back to int64
            self._snps["pos"] = self._snps["pos"].astype(np.int64)

        self.sort_snps()

        return discrepant_positions, discrepant_genotypes

    @staticmethod
    def _double_single_alleles(df, chrom):
        """ Double any single alleles in the specified chromosome.

        Parameters
        ----------
        df : pandas.DataFrame
            SNPs
        chrom : str
            chromosome of alleles to double

        Returns
        -------
        df : pandas.DataFrame
            SNPs with specified chromosome's single alleles doubled
        """
        # find all single alleles of the specified chromosome
        single_alleles = np.where(
            (df["chrom"] == chrom) & (df["genotype"].str.len() == 1)
        )[0]

        # double those alleles
        df.iloc[single_alleles, 2] = df.iloc[single_alleles, 2] * 2

        return df
