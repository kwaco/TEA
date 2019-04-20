'''
Training interface for Neural network model to detect and classify TLINKS between entities.
'''
from __future__ import print_function
import sys
import os
from src.config import env_paths
import numpy
# numpy.random.seed(1337)

# this needs to be set. exit now so user doesn't wait to know.
if env_paths()["PY4J_DIR_PATH"] is None:
    sys.exit("PY4J_DIR_PATH environment variable not specified")

import argparse
import glob
import pickle
import json

from src.learning.network_mem import NetworkMem, BATCH_SIZE
from src.notes.TimeNote import TimeNote
from src.learning.word2vec import load_word2vec_binary, build_vocab

from keras.models import model_from_json
from keras.models import load_model
from keras.callbacks import ModelCheckpoint, EarlyStopping

from src.learning.ntm_models import LABELS, DENSE_LABELS, EMBEDDING_DIM, MAX_LEN
from src.learning.time_ref import predict_timex_rel

HAS_AUX = False

def main():
    '''
    Process command line arguments and then generate trained models (One for detection of links, one for classification)
    '''

    parser = argparse.ArgumentParser()

    parser.add_argument("train_dir",
                        help="Directory containing training annotations")

    parser.add_argument("model_destination",
                        help="Where to store the trained model")

    parser.add_argument("newsreader_annotations",
                        help="Where newsreader pipeline parsed file objects go")

    parser.add_argument("--val_dir",
                        default=None,
                        help="Directory containing validation annotations")

    parser.add_argument("--load_model",
                        action='store_true',
                        default=False,
                        help="Load saved model and resume training from there")

    parser.add_argument("--no_val",
                        action='store_true',
                        default=False,
                        help="No validation. Use all training data to train.")

    parser.add_argument("--pair_type",
                        default='both',
                        help="specify the entity type to train: intra, cross or both")

    parser.add_argument("--nolink",
                        default=None,
                        type=float,
                        help="no link downsampling ratio. e.g. 0.5 means # of nolinks are 50% of # positive tlinks")

    parser.add_argument("--no_ntm",
                        action='store_true',
                        default=False,
                        help="specify whether to use neural turing machine. default is to use ntm (no_ntm=false).")

    parser.add_argument("--augment",
                        action='store_true',
                        default=False,
                        help="specify whether to use augmented data (flip pairs).")

    args = parser.parse_args()

    assert args.pair_type in ('intra', 'cross', 'dct', 'all')

    # validate file paths
    if os.path.isdir(args.newsreader_annotations) is False:
        sys.exit("invalid path for time note dir")
    if os.path.isdir(args.train_dir) is False:
        sys.exit("invalid path to directory containing training data")
    if os.path.isdir(os.path.dirname(args.model_destination)) is False:
        sys.exit("directory for model destination does not exist")

    print("arguments:\n", args)

    # get files in directory
    files = glob.glob(os.path.join(args.train_dir, '*'))
    gold_files = []
    tml_files = []

    for f in files:
        if "E3input" in f:
            tml_files.append(f)
        elif f.endswith('.tml'):
            gold_files.append(f)

    gold_files.sort()
    tml_files.sort()

    if args.val_dir is None:
        val_files = None
    else:
        val_files = glob.glob(os.path.join(args.val_dir, '*'))
        val_files.sort()

    # one-to-one pairing of annotated file and un-annotated
    # assert len(gold_files) == len(tml_files)

    N_CLASSES = len(LABELS)
    notes = get_notes(gold_files, args.newsreader_annotations, augment=args.augment)
    numpy.random.shuffle(notes)
    n = len(notes)

    if args.augment:
        splits = 14  # the estimated number of chunks we divide a batch/document into
        rounds = 4  # number of epochs to use all training data, good for fast check

        # the steps_per_epoch is useful if a single document is divided into chunks
        # if we use a whole document as a patch, it will be just the number of documents
        if args.pair_type == 'cross':
            batch_size = 350 / splits
            steps_per_epoch = 7200/batch_size + n/2  # 7188/2 entries, 22 notes
        elif args.pair_type == 'intra':
            batch_size = 200 / splits
            steps_per_epoch = 3900/batch_size + n/2  # 3880/2 entries, 22 notes
        elif args.pair_type == 'all':
            batch_size = 560 / splits
            steps_per_epoch = 12000/batch_size + n/2
        else:
            batch_size = 50
            steps_per_epoch = 1000/batch_size + n/2 # 1046/2 entries, 22 notes
        steps_per_epoch /= rounds

        if not args.no_val:
            val_notes = get_notes(val_files, args.newsreader_annotations, augment=args.augment)
            m = len(val_notes)
            if args.pair_type == 'cross':
                validation_steps = 2400/batch_size + m/2  # 2368/2 entries, 9 notes, 2000/batch_size is reasonable
            elif args.pair_type == 'intra':
                validation_steps = 1400/batch_size + m/2  # 1352/2 entries, 9 notes,
            elif args.pair_type == 'all':
                validation_steps = 4000/batch_size + m/2
            else:
                validation_steps = 300/batch_size + m/2  # 331 entries, 9 notes,
        else:
            validation_steps = None

    else:
        splits = 10  # the estimated number of chunks we divide a batch/document into
        rounds = 2  # number of epochs to use all training data, good for fast check

        # the steps_per_epoch is useful if a single document is divided into chunks
        # if we use a whole document as a patch, it will be just the number of documents
        if args.pair_type == 'cross':
            batch_size = 160 / splits
            steps_per_epoch = 3600 / batch_size + n / 2  # 7188/2 entries, 22 notes
        elif args.pair_type == 'intra':
            batch_size = 90 / splits
            steps_per_epoch = 1900 / batch_size + n / 2  # 3880/2 entries, 22 notes
        elif args.pair_type == 'all':
            batch_size = 300 / splits
            steps_per_epoch = 6500 / batch_size + n / 2
        else:
            batch_size = 50
            steps_per_epoch = 1000 / batch_size + n / 2  # 1046 entries, 22 notes
        steps_per_epoch /= rounds

        if not args.no_val:
            val_notes = get_notes(val_files, args.newsreader_annotations)
            m = len(val_notes)
            if args.pair_type == 'cross':
                validation_steps = 1200 / batch_size + m / 2
            elif args.pair_type == 'intra':
                validation_steps = 680 / batch_size + m / 2
            elif args.pair_type == 'all':
                validation_steps = 2200 / batch_size + m / 2
            else:
                validation_steps = 300 / batch_size + m / 2
        else:
            validation_steps = None

    batch_size = int(batch_size)
    print("use batch size", batch_size)
    print("steps_per_epoch", steps_per_epoch)
    print("validation_steps", validation_steps)
    # word_vectors = load_word2vec_binary(os.environ["TEA_PATH"] + '/GoogleNews-vectors-negative300.bin', verbose=0)

    network = NetworkMem(no_ntm=args.no_ntm, nb_training_files=len(notes))
    print("loading word vectors...")
    if val_notes:
        print("found notes for training and test...")
        network.build_wordvectors(notes + val_notes)
    else:
        network.build_wordvectors(notes)

    training_data_gen = network.generate_training_input(notes, args.pair_type, max_len=MAX_LEN, nolink_ratio=args.nolink, no_ntm=args.no_ntm, multiple=1)

    if not args.no_val and val_notes is not None:
        network.get_embedding_matrix()
        val_data_gen = network.generate_test_input(val_notes, args.pair_type, max_len=MAX_LEN, no_ntm=args.no_ntm, multiple=1)
    else:
        val_data_gen = None

    model_destination = os.path.join(args.model_destination, args.pair_type) + '/'
    if not os.path.exists(model_destination):
        os.makedirs(model_destination)

    if args.no_ntm:
        epochs = 150
        patience = 15
    else:
        epochs = 100
        patience = 10

    if args.no_val:
        earlystopping = EarlyStopping(monitor='loss', patience=patience, verbose=0, mode='auto')
        checkpoint = ModelCheckpoint(model_destination + 'best_weights.h5', monitor='loss', save_best_only=True, save_weights_only=True)
    else:
        earlystopping = EarlyStopping(monitor='val_acc', patience=patience, verbose=0, mode='auto')
        checkpoint = ModelCheckpoint(model_destination + 'best_weights.h5', monitor='val_loss', save_best_only=True, save_weights_only=True)
    callbacks = {'earlystopping': earlystopping, 'checkpoint': checkpoint}

    # create a sinlge model, then save architecture and weights
    if args.load_model:
        # try:
        #     model = load_model(model_destination + 'final_model.h5')
        #     # network.get_embedding_matrix()
        # except:
        model = network.load_raw_model(args.no_ntm)
        model, _ = network.train_model(model=model, no_ntm=args.no_ntm, epochs=1,
                                             input_generator=training_data_gen, val_generator=val_data_gen,
                                             weight_classes=True, callbacks=callbacks,
                                             batch_size=batch_size, has_auxiliary=HAS_AUX)
        model.load_weights(model_destination + 'final_weights.h5')
    else:
        model = None

    print("model to load", model)
    model, history = network.train_model(model=model, no_ntm=args.no_ntm, epochs=epochs,
                                         input_generator=training_data_gen, val_generator=val_data_gen,
                                         weight_classes=True, callbacks=callbacks,
                                         batch_size=batch_size, has_auxiliary=HAS_AUX)

    try:
        model.save(model_destination + 'final_model.h5')
    except:
        # with open(model_destination + 'arch.json', "wb") as f:
        #     f.write(model.to_json())
        model.save_weights(model_destination + 'final_weights.h5')
    json.dump(history, open(model_destination + 'training_history.json', 'w'))

    # evaluation

    if DENSE_LABELS:
        eval_batch = 0
    else:
        eval_batch = 160
    test_data_gen = val_data_gen
    print("Prediction results without double check in batch 40...")
    results = network.predict(model, test_data_gen, batch_size=40, fit_batch_size=BATCH_SIZE,
                              evaluation=True, smart=False, has_auxiliary=HAS_AUX, pruning=False)

    # print("Prediction from final model, with pruning and double check...")
    # # model.load_weights(model_destination + 'best_weights.h5')
    # network.predict(model, test_data_gen, batch_size=0, evaluation=True, smart=True, no_ntm=args.no_ntm,
    #                 has_auxiliary=HAS_AUX, pruning=True)

    # print("Prediction with double check in batches.")
    # results = network.predict(model, test_data_gen, batch_size=batch_size, fit_batch_size=BATCH_SIZE,
    #                           evaluation=True, smart=True, has_auxiliary=HAS_AUX, pruning=False)

    print("Prediction with double check in one batch.")
    # network.predict(model, test_data_gen, batch_size=0, evaluation=True, smart=True, no_ntm=args.no_ntm,
    #                 has_auxiliary=HAS_AUX, pruning=False)
    results = network.predict(model, test_data_gen, batch_size=eval_batch, fit_batch_size=BATCH_SIZE,
                              evaluation=True, smart=True, has_auxiliary=HAS_AUX, pruning=False)


    with open(model_destination + 'results.pkl', 'wb') as f:
        pickle.dump(results, f)

    with open(model_destination + 'vocab.pkl', 'wb') as f:
        pickle.dump(network.word_vectors, f)



    # print("Raw Prediction with batches...")
    # network.predict(model, test_data_gen, batch_size=batch_size, evaluation=True, smart=False, no_ntm=args.no_ntm,
    #                 has_auxiliary=HAS_AUX, pruning=False)

    # print("Prediction results for two-pass reading...")
    # network.test_data_collection = []
    # test_data_gen = network.generate_test_input(val_notes, args.pair_type, max_len=MAX_LEN, no_ntm=args.no_ntm,
    #                                             multiple=1)
    # network.predict(model, test_data_gen, batch_size=batch_size, evaluation=True, smart=True, no_ntm=args.no_ntm,
    #                 has_auxiliary=False, combine_timex=True)

    # architecture = model.to_json(
    #     custom_objects={'NeuralTuringMachine': NeuralTuringMachine, 'StatefulController': StatefulController})
    # open(model_destination + '.arch.json', "wb").write(architecture)

    # model.save(model_destination + 'final_model.h5')

    # if DENSE_LABELS:  # intrinsic evaluation for TimeBank-Dense
    #     network.test_data_collection = []  # need to do it because we used it for val data
    #     # do not use the internal evaluation function of predict() here
    #     predictions, scores, true_labels = network.predict(model, test_data_gen, batch_size=batch_size, evaluation=False, smart=True, no_ntm=args.no_ntm, has_auxiliary=False)
    #
    #     timex_labels, timex_pair_index = predict_timex_rel(val_notes)
    #     true_timex_labels = []
    #     pred_timex_labels = []
    #     timex_pairs = []
    #     for i, note in enumerate(val_notes):
    #
    #         id_to_labels = note.id_to_denselabels  # If augmented, this is bidirectional, even for t0 pairs
    #         processed = {}  # used to remove redundant pairs
    #
    #         # The id pairs in timex_pair_index are exactly the same as in note.timex_pairs
    #         # For TimeBank-Dense data, only labeled pairs are included
    #         for pair in note.timex_pairs:
    #             if pair in processed: continue
    #             if (i, pair) in timex_pair_index:
    #                 timex_pairs.append((i, pair))
    #                 pred_timex_labels.append(timex_labels[timex_pair_index[(i, pair)]])
    #
    #                 if pair in id_to_labels:
    #                     true_timex_labels.append(id_to_labels[pair])
    #                 else:
    #                     true_timex_labels.append(LABELS.index("None"))
    #                     print("Timex pair in %s not found in true labels:" % note.annotated_note_path, pair)
    #             else:
    #                 print("Timex pair in %s not found in timex_pair_index:" % note.annotated_note_path, pair)
    #
    #             processed[pair] = 1
    #             processed[(pair[1], pair[0])] = 1
    #     pred_timex_labels = network._convert_str_labels_to_int(pred_timex_labels)
    #     true_timex_labels = network._convert_str_labels_to_int(true_timex_labels)
    #
    #     predictions += pred_timex_labels
    #     true_labels = numpy.concatenate([true_labels, numpy.array(true_timex_labels)], axis=0)
    #     NetworkMem.class_confusion(predictions, true_labels, len(LABELS))


def basename(name):
    name = os.path.basename(name)
    name = name.replace('.TE3input', '')
    name = name.replace('.tml', '')
    return name


def get_notes(files, newsreader_dir, augment=False):

    if not files:
        return None

    notes = []

    for i, tml in enumerate(files):
        if i % 10 == 0:
            print('processing file {}/{} {}'.format(i + 1, len(files), tml))
        if os.path.isfile(os.path.join(newsreader_dir, basename(tml) + ".parsed.pickle")):
            tmp_note = pickle.load(open(os.path.join(newsreader_dir, basename(tml) + ".parsed.pickle"), "rb"))
        else:
            if DENSE_LABELS:
                if augment:
                    denselabels = pickle.load(open(newsreader_dir + 'dense-labels.pkl', 'rb'))
                else:
                    denselabels = pickle.load(open(newsreader_dir + 'dense-labels-single.pkl', 'rb'))
            else:
                denselabels = None
            tmp_note = TimeNote(tml, tml, denselabels=denselabels)
            pickle.dump(tmp_note, open(newsreader_dir + "/" + basename(tml) + ".parsed.pickle", "wb"))

        if DENSE_LABELS and tmp_note.denselabels is None: # handle old note files without dense labels
            tmp_note.denselabels = pickle.load(open(newsreader_dir + 'dense-labels.pkl'))
            tmp_note.get_id_to_denselabels()
        notes.append(tmp_note)
    return notes


if __name__ == "__main__":
    main()