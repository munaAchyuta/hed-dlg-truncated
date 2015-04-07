#!/usr/bin/env python

import argparse
import cPickle
import traceback
import logging
import time
import sys

import os
import numpy
import codecs

from dialog_encdec import DialogEncoderDecoder 
from numpy_compat import argpartition
from state import prototype_state

logger = logging.getLogger(__name__)

class Timer(object):
    def __init__(self):
        self.total = 0

    def start(self):
        self.start_time = time.time()

    def finish(self):
        self.total += time.time() - self.start_time

class BeamSearch(object):
    def __init__(self, model):
        self.model = model 
        state = self.model.state 
        self.unk_sym = self.model.unk_sym
        self.eos_sym = self.model.eos_sym
        self.eot_sym = self.model.eot_sym
        self.qdim = self.model.qdim
        self.sdim = self.model.sdim

    def compile(self):
        logger.debug("Compiling beam search functions")
        self.next_probs_predictor = self.model.build_next_probs_function()
        self.compute_encoding = self.model.build_encoder_function()

    def search(self, seq, beam_size=1, ignore_unk=False, minlen=1, normalize_by_length=True):
        # Make seq a column vector
        seq = numpy.array(seq)
        
        if seq.ndim == 1:
            seq = numpy.array([seq], dtype='int32').T
        else:
            seq = seq.T

        assert seq.ndim == 2
        h, hr, hs = self.compute_encoding(seq)
         
        # Initializing starting points with the last encoding of the sequence 
        prev_words = numpy.zeros((seq.shape[1],), dtype='int32') + self.eos_sym
        prev_hd = numpy.zeros((seq.shape[1], self.qdim), dtype='float32')
        prev_hs = numpy.zeros((seq.shape[1], self.sdim), dtype='float32')
         
        prev_hs[:] = hs[-1]
         
        fin_beam_gen = []
        fin_beam_costs = []

        beam_gen = [[]] 
        costs = [0.0]

        for k in range(100):
            if beam_size == 0:
                break

            logger.info("Beam search at step %d" % k)
            prev_words = (numpy.array(map(lambda bg : bg[-1], beam_gen))
                    if k > 0
                    else numpy.zeros(1, dtype="int32") + self.eos_sym)
             
            prev_hd = prev_hd[:beam_size]
            prev_hs = prev_hs[:beam_size]

            assert prev_hs.shape[0] == prev_hd.shape[0]
            assert prev_words.shape[0] == prev_hs.shape[0]

            outputs, hd = self.next_probs_predictor(prev_hs, prev_words, prev_hd)
            log_probs = numpy.log(outputs)

            # Adjust log probs according to search restrictions
            if ignore_unk:
                log_probs[:, self.unk_sym] = -numpy.inf

            if k < minlen:
                log_probs[:, self.eos_sym] = -numpy.inf
                log_probs[:, self.eot_sym] = -numpy.inf 

            # Find the best options by calling argpartition of flatten array
            next_costs = numpy.array(costs)[:, None] - log_probs
            flat_next_costs = next_costs.flatten()
            best_costs_indices = argpartition(
                    flat_next_costs.flatten(),
                    beam_size)[:beam_size]

            # Decypher flatten indices
            voc_size = log_probs.shape[1]
            trans_indices = best_costs_indices / voc_size
            word_indices = best_costs_indices % voc_size
            costs = flat_next_costs[best_costs_indices]

            # Form a beam for the next iteration
            new_beam_gen = [[]] * beam_size 
            new_costs = numpy.zeros(beam_size)
            
            new_prev_hs = numpy.zeros((beam_size, self.sdim), dtype="float32")
            new_prev_hs[:] = hs[-1]
            new_prev_hd = numpy.zeros((beam_size, self.qdim), dtype="float32")
            
            for i, (orig_idx, next_word, next_cost) in enumerate(
                    zip(trans_indices, word_indices, costs)):
                
                new_beam_gen[i] = beam_gen[orig_idx] + [next_word]
                new_costs[i] = next_cost
                new_prev_hd[i] = hd[orig_idx]
             
            beam_gen = []
            costs = []
            indices = []

            for i in range(beam_size):
                # We finished sampling?
                if new_beam_gen[i][-1] != self.eos_sym:
                    beam_gen.append(new_beam_gen[i])
                    costs.append(new_costs[i])
                    indices.append(i)
                else:
                    beam_size -= 1
                    fin_beam_gen.append(new_beam_gen[i])
                    if normalize_by_length:
                        fin_beam_costs.append(new_costs[i]/len(new_beam_gen[i]))

            # Filter out the finished states 
            prev_hd = new_prev_hd[indices]
            prev_hs = new_prev_hs[indices]
         
        fin_beam_gen = numpy.array(fin_beam_gen)[numpy.argsort(fin_beam_costs)]
        fin_beam_costs = numpy.array(sorted(fin_beam_costs))
         
        return fin_beam_gen, fin_beam_costs

def sample(model, seqs=[[]], n_samples=1, sampler=None, beam_search=None, ignore_unk=False, normalize=False, alpha=1, verbose=False): 
    if beam_search:
        logger.info("Starting beam search : {} start sequences in total".format(len(seqs)))
         
        for idx, seq in enumerate(seqs):
            sentences = [] 
            
            seq = model.words_to_indices(seq, add_se=True)
            logger.info("Searching for {}".format(seq))
             
            gen_queries, gen_costs = beam_search.search(seq, n_samples, ignore_unk=ignore_unk) 
              
            for i in range(len(gen_queries)):
                query = model.indices_to_words(gen_queries[i])
                sentences.append(" ".join(query))

            for i in range(len(gen_costs)):   
                print "{}: {}".format(gen_costs[i], sentences[i].encode('utf-8'))
            
        return sentences, gen_costs, gen_queries 
    elif sampler:
        logger.info("Starting sampling")
        max_length = 40         
        print sampler(n_samples, max_length) 
    else:
        raise Exception("I don't know what to do")

def parse_args():
    parser = argparse.ArgumentParser("Sample (with beam-search) from the session model")
    
    parser.add_argument("--n-samples", 
            default="1", type=int, 
            help="Number of samples, if used with --beam-search, the size of the beam")
    
    parser.add_argument("--ignore-unk",
            default=True, action="store_true",
            help="Ignore unknown words")
    
    parser.add_argument("model_prefix",
            help="Path to the model prefix (without _model.npz or _state.pkl)")
    
    parser.add_argument("queries",
            help="File of input queries")

    parser.add_argument("--normalize",
            action="store_true", default=False,
            help="Normalize log-prob with the word count")
     
    parser.add_argument("--beam-search",
            action="store_true",
            help="Enable beam search")

    parser.add_argument("--verbose",
            action="store_true", default=False,
            help="Be verbose")
    
    parser.add_argument("changes", nargs="?", default="", help="Changes to state")
    return parser.parse_args()

def main():
    args = parse_args()
    state = prototype_state()
   
    state_path = args.model_prefix + "_state.pkl"
    model_path = args.model_prefix + "_model.npz"

    with open(state_path) as src:
        state.update(cPickle.load(src)) 
    
    logging.basicConfig(level=getattr(logging, state['level']), format="%(asctime)s: %(name)s: %(levelname)s: %(message)s")
     
    model = DialogEncoderDecoder(state)
    if os.path.isfile(model_path):
        logger.debug("Loading previous model")
        model.load(model_path)
    else:
        raise Exception("Must specify a valid model path")
    
    beam_search = None
    sampler = None

    if args.beam_search:
        beam_search = BeamSearch(model)
        beam_search.compile()
    else:
        sampler = model.build_sampling_function()
     
    seqs = [[]]
    if args.queries:
        lines = open(args.queries, "r").readlines()
        seqs = [x.strip().split() for x in lines]
     
    sample(model, seqs=seqs, ignore_unk=args.ignore_unk, sampler=sampler, beam_search=beam_search, n_samples=args.n_samples)

if __name__ == "__main__":
    main()
