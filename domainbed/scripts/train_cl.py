# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

import argparse
import collections
import json
import os
import random
import sys
import time
import uuid
from itertools import chain

import numpy as np
import PIL
import torch
import torchvision
import torch.utils.data

from domainbed import datasets
from domainbed import hparams_registry
from domainbed import algorithms
from domainbed.lib import misc
from domainbed.lib.fast_data_loader import InfiniteDataLoader, FastDataLoader, DataParallelPassthrough
from domainbed import model_selection
from domainbed.lib.query import Q


def cal_warmup_support(algorithm, evals, classifier_shape, device):
    algorithm.eval()
    algorithm.to(device)
    classifier_weights = torch.zeros(classifier_shape)
    classifier_weights = classifier_weights.to(device)
    counts = [0 for i in range(classifier_weights.shape[0])]
    for name, loader, weights in evals:
        with torch.no_grad():
            for x, y in loader:
                x = x.to(device)
                y = y.to(device)
                z = algorithm.featurizer(x)
                # z2 = algorithm.featurizer(x2)
                # z = (z1 + z2)/2
                z = z.to(device)
                for i in range(len(y)):
                    classifier_weights[y[i]] += z[i]
                    counts[y[i]] += 1
    for i in range(classifier_weights.shape[0]):
        classifier_weights[i] /= max(counts[i], 1)
    return classifier_weights

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Domain generalization')
    parser.add_argument('--data_dir', type=str)
    parser.add_argument('--dataset', type=str, default="RotatedMNIST")
    parser.add_argument('--algorithm', type=str, default="ERM")
    parser.add_argument('--task', type=str, default="domain_generalization",
        help='domain_generalization | domain_adaptation')
    parser.add_argument('--hparams', type=str,
        help='JSON-serialized hparams dict')
    parser.add_argument('--hparams_seed', type=int, default=0,
        help='Seed for random hparams (0 means "default hparams")')
    parser.add_argument('--trial_seed', type=int, default=0,
        help='Trial number (used for seeding split_dataset and '
        'random_hparams).')
    parser.add_argument('--seed', type=int, default=0,
        help='Seed for everything else')
    parser.add_argument('--steps', type=int, default=None,
        help='Number of steps. Default is dataset-dependent.')
    parser.add_argument('--checkpoint_freq', type=int, default=None,
        help='Checkpoint every N steps. Default is dataset-dependent.')
    parser.add_argument('--test_envs', type=int, nargs='+', default=[0])
    parser.add_argument('--output_dir', type=str, default="train_output")
    parser.add_argument('--holdout_fraction', type=float, default=0.2)
    parser.add_argument('--uda_holdout_fraction', type=float, default=0)
    parser.add_argument('--skip_model_save', action='store_true')
    parser.add_argument('--save_model_every_checkpoint', action='store_true')
    parser.add_argument('--encoder', action='store_true')
    parser.add_argument('--clustering', action='store_true')
    parser.add_argument('--finetune_step', type=int, default=1000)
    args = parser.parse_args()

    # If we ever want to implement checkpointing, just persist these values
    # every once in a while, and then load them from disk here.
    start_step = 0
    algorithm_dict = None

    os.makedirs(args.output_dir, exist_ok=True)
    sys.stdout = misc.Tee(os.path.join(args.output_dir, 'out.txt'))
    sys.stderr = misc.Tee(os.path.join(args.output_dir, 'err.txt'))

    print("Environment:")
    print("\tPython: {}".format(sys.version.split(" ")[0]))
    print("\tPyTorch: {}".format(torch.__version__))
    print("\tTorchvision: {}".format(torchvision.__version__))
    print("\tCUDA: {}".format(torch.version.cuda))
    print("\tCUDNN: {}".format(torch.backends.cudnn.version()))
    print("\tNumPy: {}".format(np.__version__))
    print("\tPIL: {}".format(PIL.__version__))

    print('Args:')
    for k, v in sorted(vars(args).items()):
        print('\t{}: {}'.format(k, v))

    if args.hparams_seed == 0:
        hparams = hparams_registry.default_hparams(args.algorithm, args.dataset)
    else:
        hparams = hparams_registry.random_hparams(args.algorithm, args.dataset,
            misc.seed_hash(args.hparams_seed, args.trial_seed))
    if args.hparams:
        hparams.update(json.loads(args.hparams))

    print('HParams:')
    for k, v in sorted(hparams.items()):
        print('\t{}: {}'.format(k, v))

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    if torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    if args.dataset in vars(datasets):
        dataset = vars(datasets)[args.dataset](args.data_dir,
            args.test_envs, hparams)
        hparams['cl'] = False
        dataset_origin = vars(datasets)[args.dataset](args.data_dir,
            args.test_envs, hparams)
        hparams['cl'] = True
    else:
        raise NotImplementedError

    # Split each env into an 'in-split' and an 'out-split'. We'll train on
    # each in-split except the test envs, and evaluate on all splits.
    
    # To allow unsupervised domain adaptation experiments, we split each test
    # env into 'in-split', 'uda-split' and 'out-split'. The 'in-split' is used
    # by collect_results.py to compute classification accuracies.  The
    # 'out-split' is used by the Oracle model selectino method. The unlabeled
    # samples in 'uda-split' are passed to the algorithm at training time if
    # args.task == "domain_adaptation". If we are interested in comparing
    # domain generalization and domain adaptation results, then domain
    # generalization algorithms should create the same 'uda-splits', which will
    # be discared at training.
    in_splits = []
    out_splits = []
    uda_splits = []
    for env_i, env in enumerate(dataset):
        uda = []

        out, in_ = misc.split_dataset(env,
            int(len(env)*args.holdout_fraction),
            misc.seed_hash(args.trial_seed, env_i))

        if env_i in args.test_envs:
            uda, in_ = misc.split_dataset(in_,
                int(len(in_)*args.uda_holdout_fraction),
                misc.seed_hash(args.trial_seed, env_i))

        if hparams['class_balanced']:
            in_weights = misc.make_weights_for_balanced_classes(in_)
            out_weights = misc.make_weights_for_balanced_classes(out)
            if uda is not None:
                uda_weights = misc.make_weights_for_balanced_classes(uda)
        else:
            in_weights, out_weights, uda_weights = None, None, None
        in_splits.append((in_, in_weights))
        out_splits.append((out, out_weights))
        if len(uda):
            uda_splits.append((uda, uda_weights))

    in_splits_origin = []
    out_splits_origin = []
    uda_splits_origin = []

    for env_i, env in enumerate(dataset_origin):
        uda = []

        out, in_ = misc.split_dataset(env,
            int(len(env)*args.holdout_fraction),
            misc.seed_hash(args.trial_seed, env_i))

        if env_i in args.test_envs:
            uda, in_ = misc.split_dataset(in_,
                int(len(in_)*args.uda_holdout_fraction),
                misc.seed_hash(args.trial_seed, env_i))

        if hparams['class_balanced']:
            in_weights_origin = misc.make_weights_for_balanced_classes(in_)
            out_weights_origin = misc.make_weights_for_balanced_classes(out)
            if uda is not None:
                uda_weights_origin = misc.make_weights_for_balanced_classes(uda)
        else:
            in_weights_origin, out_weights_origin, uda_weights_origin = None, None, None
        in_splits_origin.append((in_, in_weights_origin))
        out_splits_origin.append((out, out_weights_origin))
        if len(uda):
            uda_splits_origin.append((uda, uda_weights_origin))


    train_loaders = [InfiniteDataLoader(
        dataset=env,
        weights=env_weights,
        batch_size=hparams['batch_size'],
        num_workers=dataset.N_WORKERS)
        for i, (env, env_weights) in enumerate(in_splits)
        if i not in args.test_envs]
    
    uda_loaders = [InfiniteDataLoader(
        dataset=env,
        weights=env_weights,
        batch_size=hparams['batch_size'],
        num_workers=dataset.N_WORKERS)
        for i, (env, env_weights) in enumerate(uda_splits)
        if i in args.test_envs]

    classifier_loaders = [InfiniteDataLoader(
        dataset=env,
        weights=env_weights,
        batch_size=hparams['batch_size'],
        num_workers=dataset.N_WORKERS)
        for i, (env, env_weights) in enumerate(in_splits_origin)
        if i not in args.test_envs]
        
    eval_weights = [None for _, weights in (in_splits + out_splits + uda_splits)]
    eval_loader_names = ['env{}_in'.format(i)
        for i in range(len(in_splits))]
    eval_loader_names += ['env{}_out'.format(i)
        for i in range(len(out_splits))]
    eval_loader_names += ['env{}_uda'.format(i)
        for i in range(len(uda_splits))]

    algorithm_class = algorithms.get_algorithm_class(args.algorithm)
    hparams['T_max'] = args.steps or dataset.N_STEPS
    algorithm = algorithm_class(dataset.input_shape, dataset.num_classes,
        len(dataset) - len(args.test_envs), hparams)

    if algorithm_dict is not None:
        algorithm.load_state_dict(algorithm_dict)

    algorithm.to(device)
    if hasattr(algorithm, 'network'):
        algorithm.network = DataParallelPassthrough(algorithm.network)
    else:
        for m in algorithm.children():
            m = DataParallelPassthrough(m)

    train_minibatches_iterator = zip(*train_loaders)
    uda_minibatches_iterator = zip(*uda_loaders)
    checkpoint_vals = collections.defaultdict(lambda: [])

    steps_per_epoch = min([len(env)/hparams['batch_size'] for env,_ in in_splits])

    n_steps = args.steps or dataset.N_STEPS
    checkpoint_freq = args.checkpoint_freq or dataset.CHECKPOINT_FREQ

    def save_checkpoint(filename):
        if args.skip_model_save:
            return
        save_dict = {
            "args": vars(args),
            "model_input_shape": dataset.input_shape,
            "model_num_classes": dataset.num_classes,
            "model_num_domains": len(dataset) - len(args.test_envs),
            "model_hparams": hparams,
            "model_dict": algorithm.cpu().state_dict()
        }
        torch.save(save_dict, os.path.join(args.output_dir, filename))

    def load_checkpoint(filename):
        if args.skip_model_save:
            return
        load_dict = torch.load(os.path.join(args.output_dir, filename))
        algorithm.load_state_dict(load_dict['model_dict'])

    # load_checkpoint('feature.pkl')
    
    # labeling_minibatches_iterator = zip(*train_loaders)
    # for step in range(n_steps):
    #     minibatches_device = [(x[0].to(device), x[1].to(device), y.to(device))
    #         for x,y in next(labeling_minibatches_iterator)]
    #     algorithm.update_classifier(minibatches_device, step)
    # algorithm.set_classifier()
    if args.encoder:
        print('start training encoder')
        last_results_keys = None
        for step in range(start_step, n_steps):
            step_start_time = time.time()
            minibatches_device = [(x[0].to(device), x[1].to(device), y.to(device))
                for x,y in next(train_minibatches_iterator)]
            if args.task == "domain_adaptation":
                uda_device = [x.to(device)
                    for x,_ in next(uda_minibatches_iterator)]
            else:
                uda_device = None
            step_vals = algorithm.update(minibatches_device, uda_device)
            checkpoint_vals['step_time'].append(time.time() - step_start_time)

            for key, val in step_vals.items():
                checkpoint_vals[key].append(val)

            if (step % checkpoint_freq == 0) or (step == n_steps - 1):
                results = {
                    'step': step,
                    'epoch': step / steps_per_epoch,
                }

                for key, val in checkpoint_vals.items():
                    results[key] = np.mean(val)

                # evals = zip(eval_loader_names, eval_loaders, eval_weights)
                # for name, loader, weights in evals:
                #     acc = misc.accuracy(algorithm, loader, weights, device)
                #     results[name+'_acc'] = acc

                results_keys = sorted(results.keys())
                if results_keys != last_results_keys:
                    misc.print_row(results_keys, colwidth=12)
                    last_results_keys = results_keys
                misc.print_row([results[key] for key in results_keys],
                    colwidth=12)

                results.update({
                    'hparams': hparams,
                    'args': vars(args)    
                })

                epochs_path = os.path.join(args.output_dir, 'results.jsonl')
                with open(epochs_path, 'a') as f:
                    f.write(json.dumps(results, sort_keys=True) + "\n")

                algorithm_dict = algorithm.state_dict()
                start_step = step + 1
                checkpoint_vals = collections.defaultdict(lambda: [])
                
                if args.save_model_every_checkpoint:
                    save_checkpoint(f'model_step{step}.pkl')
        save_checkpoint('feature.pkl')
    else:
        load_checkpoint('feature.pkl')

    classifier_minibatch_loaders = zip(*classifier_loaders)
                    
    if args.clustering:
        print('start compute class mean')
        classifier_shape = (algorithm.classifier.out_features, algorithm.classifier.in_features)
        classifier_weights = torch.zeros(classifier_shape).to(device)
        counts = [0 for i in range(classifier_weights.shape[0])]
        for step in range(args.finetune_step):
            minibatches = [(x.to(device), y.to(device))
                        for x,y in next(classifier_minibatch_loaders)]
            with torch.no_grad():
                all_x = torch.cat([x for x, y in minibatches])
                all_y = torch.cat([y for x, y in minibatches])
                z = algorithm.featurizer(all_x)
                z = z.to(device)
            for i in range(len(all_y)):
                classifier_weights[all_y[i]] += z[i]
                counts[all_y[i]] += 1
        for i in range(classifier_weights.shape[0]):
            classifier_weights[i] /= max(counts[i], 1)

        algorithm.classifier.weight = torch.nn.parameter.Parameter(classifier_weights)
        algorithm.classifier.bias = torch.nn.parameter.Parameter(torch.empty(algorithm.classifier.out_features))

        save_checkpoint('model.pkl')
    else:
        algorithm.train()
        optimizer = torch.optim.SGD(
            algorithm.classifier.parameters(),
            lr=1e-2,
            weight_decay=algorithm.hparams['weight_decay']
        )
        for step in range(args.finetune_step):
            minibatches = [(x.to(device), y.to(device))
                        for x,y in next(classifier_minibatch_loaders)]
            with torch.no_grad():
                all_x = torch.cat([x for x, y in minibatches])
                all_y = torch.cat([y for x, y in minibatches])
            loss = torch.nn.functional.cross_entropy(algorithm.network(all_x), all_y)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        save_checkpoint('model.pkl')


    with open(os.path.join(args.output_dir, 'done'), 'w') as f:
        f.write('done')
