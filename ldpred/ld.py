"""
Various useful LD functions.


"""
import scipy as sp
import sys, os, gzip, pickle
import re
# Wallace change pickle to cPickle.
# import sys, os, gzip
# import _pickle as pickle
# https://github.com/telegraphic/hickle
import hickle as hkl

import time
import h5py
from numpy import linalg
from ldpred import util
import glob



def get_LDpred_ld_tables(snps, ld_radius=100, ld_window_size=0, h2=None, n_training=None, gm=None, gm_ld_radius=None):
    """
    Calculates LD tables, and the LD score in one go...
    """

    ld_dict = {}
    m, n = snps.shape
    ld_scores = sp.ones(m)
    ret_dict = {}
    if gm_ld_radius is None:
        for snp_i, snp in enumerate(snps):
            # Calculate D
            start_i = max(0, snp_i - ld_radius)
            stop_i = min(m, snp_i + ld_radius + 1)
            X = snps[start_i: stop_i]
            D_i = sp.dot(snp, X.T) / n
            r2s = D_i ** 2
            ld_dict[snp_i] = D_i
            lds_i = sp.sum(r2s - (1 - r2s) / (n - 2), dtype='float32')
            ld_scores[snp_i] = lds_i
    else:
        assert gm is not None, 'Genetic map is missing.'
        window_sizes = []
        ld_boundaries = []
        for snp_i, snp in enumerate(snps):
            curr_cm = gm[snp_i]

            # Now find lower boundary
            start_i = snp_i
            min_cm = gm[snp_i]
            while start_i > 0 and min_cm > curr_cm - gm_ld_radius:
                start_i = start_i - 1
                min_cm = gm[start_i]

            # Now find the upper boundary
            stop_i = snp_i
            max_cm = gm[snp_i]
            while stop_i > 0 and max_cm < curr_cm + gm_ld_radius:
                stop_i = stop_i + 1
                max_cm = gm[stop_i]

            ld_boundaries.append([start_i, stop_i])
            curr_ws = stop_i - start_i
            window_sizes.append(curr_ws)
            assert curr_ws > 0, 'Some issues with the genetic map'

            X = snps[start_i: stop_i]
            D_i = sp.dot(snp, X.T) / n
            r2s = D_i ** 2
            ld_dict[snp_i] = D_i
            lds_i = sp.sum(r2s - (1 - r2s) / (n - 2), dtype='float32')
            ld_scores[snp_i] = lds_i

        avg_window_size = sp.mean(window_sizes)
        print('Average # of SNPs in LD window was %0.2f' % avg_window_size)
        if ld_window_size == 0:
            ld_window_size = avg_window_size * 2
        ret_dict['ld_boundaries'] = ld_boundaries
    ret_dict['ld_dict'] = ld_dict
    ret_dict['ld_scores'] = ld_scores

    if ld_window_size > 0:
        ref_ld_matrices = []
        inf_shrink_matrices = []
        for wi in range(0, m, ld_window_size):
            start_i = wi
            stop_i = min(m, wi + ld_window_size)
            curr_window_size = stop_i - start_i
            X = snps[start_i: stop_i]
            D = sp.dot(X, X.T) / n
            ref_ld_matrices.append(D)
            if h2 != None and n_training != None:
                A = ((m / h2) * sp.eye(curr_window_size) + (n_training / (1)) * D)
                A_inv = linalg.pinv(A)
                inf_shrink_matrices.append(A_inv)
        ret_dict['ref_ld_matrices'] = ref_ld_matrices
        if h2 != None and n_training != None:
            ret_dict['inf_shrink_matrices'] = inf_shrink_matrices
    return ret_dict


def calc_ld_table(snps, max_ld_dist=2000, min_r2=0.2, verbose=True, normalize=False):
    """
    Calculate LD between all SNPs using a sliding LD square

    This function only retains r^2 values above the given threshold
    """
    # Normalize SNPs (perhaps not necessary, but cheap)
    if normalize:
        snps = snps.T
        snps = (snps - sp.mean(snps, 0)) / sp.std(snps, 0)
        snps = snps.T


    if verbose:
        print('Calculating LD table')
    t0 = time.time()
    num_snps, num_indivs = snps.shape
    ld_table = {}
    for i in range(num_snps):
        ld_table[i] = {}

    a = min(max_ld_dist, num_snps)
    num_pairs = (a * (num_snps - 1)) - a * (a + 1) * 0.5
    if verbose:
        print('Correlation between %d pairs will be tested' % num_pairs)
    num_stored = 0
    for i in range(0, num_snps - 1):
        start_i = i + 1
        end_i = min(start_i + max_ld_dist, num_snps)
        ld_vec = sp.dot(snps[i], sp.transpose(snps[start_i:end_i])) / float(num_indivs)
        ld_vec = sp.array(ld_vec).flatten()
        for k in range(start_i, end_i):
            ld_vec_i = k - start_i
            if ld_vec[ld_vec_i] ** 2 > min_r2:
                ld_table[i][k] = ld_vec[ld_vec_i]
                ld_table[k][i] = ld_vec[ld_vec_i]
                num_stored += 1
        if verbose:
            if i % 1000 == 0:
                sys.stdout.write('.')
#                 sys.stdout.write('\b\b\b\b\b\b\b%0.2f%%' % (100.0 * (min(1, float(i + 1) / (num_snps - 1)))))
                sys.stdout.flush()
    if verbose:
        sys.stdout.write('Done.\n')
        if num_pairs > 0:
            print('Stored %d (%0.4f%%) correlations that made the cut (r^2>%0.3f).' % (num_stored, 100 * (num_stored / float(num_pairs)), min_r2))
        else:
            print('-')
    t1 = time.time()
    t = (t1 - t0)
    if verbose:
        print('\nIt took %d minutes and %0.2f seconds to calculate the LD table' % (t / 60, t % 60))
    del snps
    return ld_table


def smart_ld_pruning(scores, ld_table, max_ld=0.5, verbose=False, reverse=False):
    """
    Prunes SNPs in LD, but with smaller scores (p-values or betas)

    If using betas, set reversed to True.
    """
    if verbose:
        print('Performing smart LD pruning')
    t0 = time.time()
    if type(scores) == type([]):
        l = list(zip(scores, list(range(len(scores)))))
    else:
        l = list(zip(scores.tolist(), list(range(len(scores)))))
    l.sort(reverse=reverse)
    l = list(map(list, list(zip(*l))))
    rank_order = l[1]
    indices_to_keep = []
    remaining_indices = set(rank_order)
    for i in rank_order:
        if len(remaining_indices) == 0:
            break
        elif not (i in remaining_indices):
            continue
        else:
            indices_to_keep.append(i)
            for j in ld_table[i]:
                if ld_table[i][j] > max_ld and j in remaining_indices:
                    remaining_indices.remove(j)
    indices_to_keep.sort()
    pruning_vector = sp.zeros(len(scores), dtype='bool8')
    pruning_vector[indices_to_keep] = 1
    t1 = time.time()
    t = (t1 - t0)
    if verbose:
        print('\nIt took %d minutes and %0.2f seconds to LD-prune' % (t / 60, t % 60))
    return pruning_vector


def get_ld_dict(cord_data_file, local_ld_file_prefix, ld_radius, gm_ld_radius=None, wallaceld=False):
#def get_ld_dict(cord_data_file, local_ld_file_prefix, ld_radius, gm_ld_radius=None):
    """
    Returns the LD dictionary.  Creates a new LD file, if the file doesn't already exist.
    """
    local_ld_dict_file = '%s_ldradius%d.pickled.gz'%(local_ld_file_prefix, ld_radius)
    if wallaceld==False:
        if not os.path.isfile(local_ld_dict_file):
            sys.stderr.write('ERROR: Please "LDpred ldfile" to generate local ld file first.')
            sys.exit(-1)

    if not os.path.isfile(local_ld_dict_file):
        chrom_ld_scores_dict = {}
        chrom_ld_dict = {}
        chrom_ref_ld_mats = {}
        if gm_ld_radius is not None:
            chrom_ld_boundaries = {}
        ld_score_sum = 0
        num_snps = 0
        print('Calculating LD information w. radius %d' % ld_radius)

        df = h5py.File(cord_data_file)
        cord_data_g = df['cord_data']

        for chrom_str in cord_data_g:
            print('Calculating LD for chromosome %s' % chrom_str)
            g = cord_data_g[chrom_str]
            if 'raw_snps_ref' in g:
                raw_snps = g['raw_snps_ref'][...]
                snp_stds = g['snp_stds_ref'][...]
                snp_means = g['snp_means_ref'][...]


            # Filter monomorphic SNPs
            ok_snps_filter = snp_stds > 0
            ok_snps_filter = ok_snps_filter.flatten()
            raw_snps = raw_snps[ok_snps_filter]
            snp_means = snp_means[ok_snps_filter]
            snp_stds = snp_stds[ok_snps_filter]

            n_snps = len(raw_snps)
            snp_means.shape = (n_snps, 1)
            snp_stds.shape = (n_snps, 1)


            # Normalize SNPs..
            snps = sp.array((raw_snps - snp_means) / snp_stds, dtype='float32')
            assert snps.shape == raw_snps.shape, 'Problems normalizing SNPs (array shape mismatch).'
            if gm_ld_radius is not None:
                assert 'genetic_map' in g, 'Genetic map is missing.'
                gm = g['genetic_map'][...]
                ret_dict = get_LDpred_ld_tables(snps, gm=gm, gm_ld_radius=gm_ld_radius)
                chrom_ld_boundaries[chrom_str] = ret_dict['ld_boundaries']
            else:
                ret_dict = get_LDpred_ld_tables(snps, ld_radius=ld_radius, ld_window_size=2 * ld_radius)
            chrom_ld_dict[chrom_str] = ret_dict['ld_dict']
            chrom_ref_ld_mats[chrom_str] = ret_dict['ref_ld_matrices']
            ld_scores = ret_dict['ld_scores']
            chrom_ld_scores_dict[chrom_str] = {'ld_scores':ld_scores, 'avg_ld_score':sp.mean(ld_scores)}
            ld_score_sum += sp.sum(ld_scores)
            num_snps += n_snps

            # - Wallace start
            # need modify here for save chr by chr summary.
            # WRITE OUT CHROMOSOME LEVEL data.
            betas = g['betas'][...][ok_snps_filter]
            if wallaceld==True:
                with open(local_ld_dict_file + '_byFileCache' +'.txt','w') as f:
                    f.write(chrom_str +': ld_scores\t%f\tn_snps\t%d\ttotal_beta_square\t%f\tn_betas\t%d\n'%(sp.sum(ld_scores),n_snps,sp.sum(betas ** 2),n_snps))
            # - Wallace end.

        avg_gw_ld_score = ld_score_sum / float(num_snps)
        ld_scores_dict = {'avg_gw_ld_score': avg_gw_ld_score, 'chrom_dict':chrom_ld_scores_dict}

        print('Done calculating the LD table and LD score, writing to file: %s'%local_ld_dict_file)
        ld_dict = {'ld_scores_dict':ld_scores_dict, 'chrom_ld_dict':chrom_ld_dict, 'chrom_ref_ld_mats':chrom_ref_ld_mats}
        if gm_ld_radius is not None:
            ld_dict['chrom_ld_boundaries'] = chrom_ld_boundaries
        # f = gzip.open(local_ld_dict_file, 'wb')
        # #pickle.dump(ld_dict, f, protocol=2)
        # # Wallace: use the latest pickle protocol version.
        # pickle.dump(ld_dict, f, protocol=-1)
        # f.close()

        #try hkl
        hkl.dump(ld_dict, local_ld_dict_file, mode='w', compression='gzip')

        print('LD information is now pickled.')
    else:
        print('Loading LD information from file: %s' % local_ld_dict_file)
        # f = gzip.open(local_ld_dict_file, 'r')
        # ld_dict = pickle.load(f)
        # f.close()
        ld_dict = hkl.load(local_ld_dict_file)
    return ld_dict


def get_chromosome_herits(cord_data_g, ld_scores_dict, n, max_h2=1, h2=None):
    """
    Calculating genome-wide heritability using LD score regression, and partition heritability by chromosome
    """
    num_snps = 0        # Need load this from file from coord step.
    sum_beta2s = 0      # Need load this from file from coord step.
    herit_dict = {}
    for chrom_str in util.chromosomes_list:
        if chrom_str in cord_data_g:
            g = cord_data_g[chrom_str]
            betas = g['betas'][...]
            n_snps = len(betas)
            num_snps += n_snps
            sum_beta2s += sp.sum(betas ** 2)
            herit_dict[chrom_str] = n_snps

    print('%d SNP effects were found' % num_snps)

    L = ld_scores_dict['avg_gw_ld_score']
    chi_square_lambda = sp.mean(n * sum_beta2s / float(num_snps))
    print('Genome-wide lambda inflation: %0.4f'% chi_square_lambda)
    print('Genome-wide mean LD score: %0.4f'% L)
    gw_h2_ld_score_est = max(0.0001, (max(1, chi_square_lambda) - 1) / (n * (L / num_snps)))
    print('LD-score estimated genome-wide heritability: %0.4f'% gw_h2_ld_score_est)
    assert chi_square_lambda>1, 'Something is wrong with the GWAS summary statistics, parsing of them, or the given GWAS sample size (N). Lambda (the mean Chi-square statistic) is too small.  '

    #Only use LD score heritability if it is not given as argument.
    if h2==None:
        if gw_h2_ld_score_est>1:
            print ('LD-score estimated heritability is suspiciously large, suggesting that the given sample size is wrong, or that SNPs are enriched for heritability (e.g. using p-value thresholding). If the SNPs are enriched for heritability we suggest using the --h2 flag to provide a more reasonable heritability estimate.')
        h2 = min(gw_h2_ld_score_est,max_h2)
    print('Heritability used for inference: %0.4f'%h2)

    #Distributing heritabilities among chromosomes.
    for k in herit_dict:
        herit_dict[k] = h2 * herit_dict[k]/float(num_snps)

    herit_dict['gw_h2_ld_score_est'] = gw_h2_ld_score_est
    return herit_dict

def get_chromosome_herits_wallace(cord_data_g, ld_scores_dict, n, max_h2=1, h2=None, local_ld_dict_file=None):
    """
    Calculating genome-wide heritability using LD score regression, and partition heritability by chromosome
    """
    num_snps = 0        # Need load this from file from coord step.
    sum_beta2s = 0      # Need load this from file from coord step.
    herit_dict = {}
    for chrom_str in util.chromosomes_list:
        if chrom_str in cord_data_g:
            g = cord_data_g[chrom_str]
            betas = g['betas'][...]
            n_snps = len(betas)
            num_snps += n_snps
            sum_beta2s += sp.sum(betas ** 2)
            herit_dict[chrom_str] = n_snps

    # - Wallace's version.
    # load chr specific summary from cached files.
    # load and calculate genome wide avg_gw_ld_score and chi_square_lambda
    # input files, load all the files in ld_file folder end by _byFileCache.txt
    # print local_ld_dict_file
    #loadname = os.path.dirname(os.path.realpath(local_ld_dict_file)) + '/*_byFileCache.txt'
    # better pattern recognition.
    myf = os.path.realpath(local_ld_dict_file)
    wdirname = os.path.dirname(myf)
    wfname = re.split(r'chr[0-9x]{1,2}',os.path.basename(myf),flags=re.IGNORECASE)
    if len(wfname) == 1:
        sys.stderr.write('ERROR: Can not find key world r"chr[0-9x]{1,2}"(case insensite) from file %s\n'(os.path.basename(x)))
        sys.exit(-1)
    loadname = wdirname + '/' + '*'.join(wfname) + '_byFileCache.txt'
    print('WALLACE INFO: load chromosome level summary file pattern: ' + loadname)
    wallace_chr_summary = []
    print('WALLACE INFO: *** please make sure all files have been loaded!****')
    ldfiles = []
    for f in glob.glob(loadname):
        # print 'WALLACE INFO: load chromosome level summary file: ' + f
        ldfiles.append(str(f))
        with open(f,'r') as rf:
            wallace_chr_summary.append(rf.readline().strip().split())

    for temp_n in sorted(ldfiles):
         print('WALLACE INFO: load chromosome level summary file: ' + temp_n)
    print('WALLACE INFO: totally loaded chr: %d'%(len(wallace_chr_summary)))

    Total_LD_scores = sum([float(x[2]) for x in wallace_chr_summary])
    Total_SNPS      = sum([int(x[4])   for x in wallace_chr_summary])
    Total_betas     = sum([float(x[6]) for x in wallace_chr_summary])
    # do not set n_snps here, this need to be set chr by chr.confirmed bug by DLpred author.
    # n_snps          = min([int(x[4])   for x in wallace_chr_summary]) # the length for chr22.
    # reset from the loading from cache file.
    num_snps        = Total_SNPS
    sum_beta2s      = Total_betas

    L = Total_LD_scores / Total_SNPS
    chi_square_lambda = sp.mean(n * sum_beta2s / float(num_snps))

    print('%d SNP effects were found' % num_snps)

    # the avg_gw_ld_score was calculated by:
    # avg_gw_ld_score = ld_score_sum / float(num_snps)
    # So we do it here for each chr.
    # OLD: L = ld_scores_dict['avg_gw_ld_score']
    # OLD: chi_square_lambda = sp.mean(n * sum_beta2s / float(num_snps))

    print('Genome-wide lambda inflation: %0.4f'% chi_square_lambda)
    print('Genome-wide mean LD score: %0.4f'% L)
    gw_h2_ld_score_est = max(0.0001, (max(1, chi_square_lambda) - 1) / (n * (L / num_snps)))
    print('LD-score estimated genome-wide heritability: %0.4f'% gw_h2_ld_score_est)
    assert chi_square_lambda>1, 'Something is wrong with the GWAS summary statistics, parsing of them, or the given GWAS sample size (N). Lambda (the mean Chi-square statistic) is too small.  '

    #Only use LD score heritability if it is not given as argument.
    if h2==None:
        if gw_h2_ld_score_est>1:
            print ('LD-score estimated heritability is suspiciously large, suggesting that the given sample size is wrong, or that SNPs are enriched for heritability (e.g. using p-value thresholding). If the SNPs are enriched for heritability we suggest using the --h2 flag to provide a more reasonable heritability estimate.')
        h2 = min(gw_h2_ld_score_est,max_h2)
    print('Heritability used for inference: %0.4f'%h2)

    #Distributing heritabilities among chromosomes.
    for k in herit_dict:
        herit_dict[k] = h2 * herit_dict[k]/float(num_snps)

    herit_dict['gw_h2_ld_score_est'] = gw_h2_ld_score_est
    return herit_dict
