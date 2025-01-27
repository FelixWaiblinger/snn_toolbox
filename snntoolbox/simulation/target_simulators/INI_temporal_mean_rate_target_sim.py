# -*- coding: utf-8 -*-
"""INI simulator with temporal mean rate code.

@author: rbodo
"""

import os
import sys

from tensorflow import keras
import numpy as np

from snntoolbox.utils.utils import IoU
from snntoolbox.parsing.utils import get_inbound_layers_with_params
from snntoolbox.simulation.utils import AbstractSNN, remove_name_counter

remove_classifier = False


class SNN(AbstractSNN):
    """
    The compiled spiking neural network, using layers derived from
    Keras base classes (see
    `snntoolbox.simulation.backends.inisim.temporal_mean_rate_tensorflow`).

    Aims at simulating the network on a self-implemented Integrate-and-Fire
    simulator using a timestepped approach.

    Attributes
    ----------

    snn: keras.models.Model
        Keras model. This is the output format of the compiled spiking model
        because INI simulator runs networks of layers that are derived from
        Keras layer base classes.
    """

    def __init__(self, config, queue=None):

        AbstractSNN.__init__(self, config, queue)

        self.snn = None
        self._spiking_layers = {}
        self._input_images = None
        self._binary_activation = None

    @property
    def is_parallelizable(self):
        return True

    def add_input_layer(self, input_shape):
        self._input_images = keras.layers.Input(batch_shape=input_shape)
        self._spiking_layers[self.parsed_model.layers[0].name] = \
            self._input_images

    def add_layer(self, layer):
        from snntoolbox.parsing.utils import get_type
        spike_layer_name = getattr(self.sim, 'Spike' + get_type(layer))
        # noinspection PyProtectedMember
        inbound = layer._inbound_nodes[0].inbound_layers
        if not isinstance(inbound, (list, tuple)):
            inbound = [inbound]
        inbound = [self._spiking_layers[inb.name] for inb in inbound]
        if len(inbound) == 1:
            inbound = inbound[0]
        layer_kwargs = layer.get_config()
        layer_kwargs['config'] = self.config

        # Check if layer uses binary activations. In that case, we will want to
        # tell the following to MaxPool layer because then we can use a
        # cheaper operation.
        if 'Conv' in layer.name and 'binary' in layer.activation.__name__:
            self._binary_activation = layer.activation.__name__

        if 'MaxPool' in layer.name and self._binary_activation is not None:
            layer_kwargs['activation'] = self._binary_activation
            self._binary_activation = None

        # Replace activation from kwargs by 'linear' before initializing
        # superclass, because the relu activation is applied by the spike-
        # generation mechanism automatically. In some cases (quantized
        # activation), we need to apply the activation manually. This
        # information is taken from the 'activation' key during conversion.
        activation_str = str(layer_kwargs.pop(str('activation'), None))

        spike_layer = spike_layer_name(**layer_kwargs)
        spike_layer.activation_str = activation_str
        spike_layer.is_first_spiking = \
            len(get_inbound_layers_with_params(layer)) == 0
        self._spiking_layers[layer.name] = spike_layer(inbound)

    def build_dense(self, layer):
        pass

    def build_convolution(self, layer):
        pass

    def build_pooling(self, layer):
        pass

    def compile(self):  # changed
        outputs = []
        for name in self._spiking_layers.keys():
            if 'class_label' in name or 'bounding_box' in name:
                outputs.append(self._spiking_layers[name])

        if len(outputs) == 1:
            outputs = outputs[0]
            loss = {'class_label': 'categorical_crossentropy'}
            metric = {'class_label': 'accuracy'}
        else:
            loss = {'class_label': 'categorical_crossentropy',
                    'bounding_box': 'mean_squared_error'}
            metric = {'class_label': 'accuracy', 'bounding_box': IoU}

        self.snn = keras.models.Model(self._input_images, outputs)
        # self._spiking_layers[self.parsed_model.layers[-1].name])
        self.snn.compile('sgd', loss=loss, metrics=metric)

        # Tensorflow 2 lists all variables as weights, including our state
        # variables (membrane potential etc). So a simple
        # snn.set_weights(parsed_model.get_weights()) does not work any more.
        # Need to extract the actual weights here.

        parameter_map = {remove_name_counter(p.name): v for p, v in
                         zip(self.parsed_model.weights,
                             self.parsed_model.get_weights())}

        count = 0
        for p in self.snn.weights:
            name = remove_name_counter(p.name)
            if name in parameter_map:
                keras.backend.set_value(p, parameter_map[name])
                count += 1

        assert count == len(parameter_map), "Not all weights have been " \
                                            "transferred from ANN to SNN."

        for layer in self.snn.layers:
            if hasattr(layer, 'bias'):
                # Adjust biases to time resolution of simulator.
                bias = keras.backend.get_value(layer.bias) * self._dt
                keras.backend.set_value(layer.bias, bias)
                if self.config.getboolean('cell', 'bias_relaxation'):
                    keras.backend.set_value(
                        layer.b0, keras.backend.get_value(layer.bias))

    def simulate(self, **kwargs):

        from snntoolbox.utils.utils import echo
        from snntoolbox.simulation.utils import get_layer_synaptic_operations

        input_b_l = kwargs[str('x_b_l')] * self._dt

        # Optionally stop simulation of current batch when number of input
        # spikes exceeds a given limit.
        num_timesteps = self._get_timestep_at_spikecount(input_b_l)

        output_b_l_t = np.zeros((self.batch_size, self.num_classes,
                                 self._num_timesteps))
        if self._detector:
            output_b_bb_t = np.zeros((self.batch_size, 4, self._num_timesteps))

        print("Current accuracy of batch:")

        # Loop through simulation time.
        self._input_spikecount = 0
        time2output = num_timesteps
        for sim_step_int in range(num_timesteps):
            sim_step = (sim_step_int + 1) * self._dt
            self.set_time(sim_step)

            # Generate new input in case it changes with each simulation step.
            if self._poisson_input:
                input_b_l = self.get_poisson_frame_batch(kwargs[str('x_b_l')])
            elif self._is_aedat_input:
                input_b_l = kwargs[str('dvs_gen')].next_eventframe_batch()

            if self._is_early_stopping and np.count_nonzero(input_b_l) == 0:
                print("\nInput empty: Finishing simulation {} steps early."
                      "".format(self._num_timesteps - sim_step_int))
                break

            # Main step: Propagate input through network and record output
            # spikes.
            out_spikes = self.snn.predict_on_batch(input_b_l)

            if self._detector:
                out_box_spikes, out_spikes = out_spikes

            # Add current spikes to previous spikes.
            if remove_classifier:  # Need to flatten output.
                output_b_l_t[:, :, sim_step_int] = np.argmax(np.reshape(
                    out_spikes > 0, (out_spikes.shape[0], -1)), 1)
            else:
                output_b_l_t[:, :, sim_step_int] = out_spikes > 0

            if self._detector:
                output_b_bb_t[:, :, sim_step_int] = out_box_spikes > 0

            # Record neuron variables.
            i = j = 0
            for layer in self.snn.layers:
                # Excludes Input, Flatten, Concatenate, etc:
                if hasattr(layer, 'spiketrain') \
                        and layer.spiketrain is not None:
                    spiketrains_b_l = keras.backend.get_value(layer.spiketrain)
                    if self.spiketrains_n_b_l_t is not None:
                        self.spiketrains_n_b_l_t[i][0][
                            Ellipsis, sim_step_int] = spiketrains_b_l
                    if self.synaptic_operations_b_t is not None:
                        self.synaptic_operations_b_t[:, sim_step_int] += \
                            get_layer_synaptic_operations(spiketrains_b_l,
                                                          self.fanout[i + 1])
                    if self.neuron_operations_b_t is not None:
                        self.neuron_operations_b_t[:, sim_step_int] += \
                            self.num_neurons_with_bias[i + 1]
                    i += 1
                if hasattr(layer, 'mem') and self.mem_n_b_l_t is not None:
                    self.mem_n_b_l_t[j][0][Ellipsis, sim_step_int] = \
                        keras.backend.get_value(layer.mem)
                    j += 1

            if 'input_b_l_t' in self._log_keys:
                self.input_b_l_t[Ellipsis, sim_step_int] = input_b_l
            if self._poisson_input or self._is_aedat_input:
                if self.synaptic_operations_b_t is not None:
                    self.synaptic_operations_b_t[:, sim_step_int] += \
                        get_layer_synaptic_operations(input_b_l,
                                                      self.fanout[0])
            else:
                if self.neuron_operations_b_t is not None:
                    if sim_step_int == 0:
                        self.neuron_operations_b_t[:, 0] += self.fanin[1] * \
                            self.num_neurons[1] * np.ones(self.batch_size) * 2

            # classification
            spike_sums_b_l = np.sum(output_b_l_t, 2)
            undecided_b = np.sum(spike_sums_b_l, 1) == 0
            guesses_b = np.argmax(spike_sums_b_l, 1)
            none_class_b = -1 * np.ones(self.batch_size)
            clean_guesses_b = np.where(undecided_b, none_class_b, guesses_b)
            current_acc = np.mean(kwargs[str('truth_b')] == clean_guesses_b)
            if self.config.getint('output', 'verbose') > 0 \
                    and sim_step % 1 == 0:
                echo('{:.2%}_'.format(current_acc))
            else:
                sys.stdout.write('\r{:>7.2%}'.format(current_acc))
                sys.stdout.flush()

            # bounding box
            if self._detector:
                bb_spike_sums_b = np.sum(output_b_bb_t, 2)
                if time2output == num_timesteps and \
                   np.max(bb_spike_sums_b) > 0:
                    time2output = num_timesteps - int(sim_step / self._dt - 1)
                bb_spike_rate_b = bb_spike_sums_b / time2output

        if self._is_aedat_input:
            remaining_events = \
                kwargs[str('dvs_gen')].remaining_events_of_current_batch()
        elif self._poisson_input and self._num_poisson_events_per_sample > 0:
            remaining_events = self._num_poisson_events_per_sample - \
                self._input_spikecount
        else:
            remaining_events = 0
        if remaining_events > 0:
            print("\nSNN Toolbox WARNING: Simulation of current batch "
                  "finished, but {} input events were not processed. Consider "
                  "increasing the simulation time.".format(remaining_events))

        if self._detector:
            return np.cumsum(output_b_l_t, 2), bb_spike_rate_b
        else:
            return np.cumsum(output_b_l_t, 2)

    def reset(self, sample_idx):

        for layer in self.snn.layers[1:]:  # Skip input layer
            layer.reset(sample_idx)

    def end_sim(self):
        pass

    def save(self, path, filename):

        filepath = str(os.path.join(path, filename + '.h5'))
        print("Saving model to {}...\n".format(filepath))
        self.snn.save(filepath, self.config.getboolean('output', 'overwrite'))

    def load(self, path, filename):

        from snntoolbox.simulation.backends.inisim. \
            temporal_mean_rate_tensorflow import custom_layers

        filepath = os.path.join(path, filename + '.h5')

        try:
            self.snn = keras.models.load_model(filepath, custom_layers)
        except KeyError:
            raise NotImplementedError(
                "Loading SNN for INIsim is not supported yet.")
            # Loading does not work anymore because the configparser object
            # needed by the custom layers is not stored when saving the model.
            # Could be implemented by overriding Keras' save / load methods,
            # but since converting even large Keras models from scratch is so
            # fast, there's really no need.

    def get_poisson_frame_batch(self, x_b_l):
        """Get a batch of Poisson input spikes.

        Parameters
        ----------

        x_b_l: ndarray
            The input frame. Shape: (`batch_size`, ``layer_shape``).

        Returns
        -------

        input_b_l: ndarray
            Array of Poisson input spikes, with same shape as ``x_b_l``.

        """

        if self._input_spikecount < self._num_poisson_events_per_sample \
                or self._num_poisson_events_per_sample < 0:
            spike_snapshot = np.random.random_sample(x_b_l.shape) \
                             * self.rescale_fac * np.max(x_b_l)
            input_b_l = (spike_snapshot <= np.abs(x_b_l)).astype('float32')
            self._input_spikecount += \
                int(np.count_nonzero(input_b_l) / self.batch_size)
            # For BinaryNets, with input that is not normalized and
            # not all positive, we stimulate with spikes of the same
            # size as the maximum activation, and the same sign as
            # the corresponding activation. Is there a better
            # solution?
            input_b_l *= np.max(x_b_l) * np.sign(x_b_l)
        else:  # No more input spikes if _input_spikecount exceeded limit.
            input_b_l = np.zeros(x_b_l.shape)

        return input_b_l

    def set_time(self, t):
        """Set the simulation time variable of all layers in the network.

        Parameters
        ----------

        t: float
            Current simulation time.
        """

        for layer in self.snn.layers[1:]:
            if layer.get_time() is not None:  # Has time attribute
                layer.set_time(np.float32(t))

    def set_spiketrain_stats_input(self):
        # Added this here because PyCharm complains about not all abstract
        # methods being implemented (even though this is not abstract).
        AbstractSNN.set_spiketrain_stats_input(self)

    def get_spiketrains_input(self):
        # Added this here because PyCharm complains about not all abstract
        # methods being implemented (even though this is not abstract).
        AbstractSNN.get_spiketrains_input(self)

    def scale_first_layer_parameters(self, t, input_b_l, tau=1):
        w, b = self.snn.layers[0].get_weights()
        alpha = (self._duration + tau) / (t + tau)
        beta = b + tau * (self._duration - t) / (t + tau) * w * input_b_l
        keras.backend.set_value(self.snn.layers[0].kernel, alpha * w)
        keras.backend.set_value(self.snn.layers[0].bias, beta)

    def _get_timestep_at_spikecount(self, x):
        """Compute timestep at which a given number of input spikes is reached.

        If the user hasn't set the ``max_num_input_spikes`` parameter in the
        config file, the simulation duration will not change.

        Otherwise, we compute the number of steps required to reach the desired
        number of spikes, which can be used to limit the simulation duration.

        Currently only works with input in the form of constant bias currents,
        not DVS or Poisson input.

        Only supports reset by subtraction for now.
        """

        max_spikecount = self.config.getint('input', 'max_num_input_spikes',
                                            fallback='')
        if max_spikecount == '':
            return self._num_timesteps

        if self._is_aedat_input or self._poisson_input or \
                self.config.get('cell', 'reset') != 'Reset by subtraction':
            # raise NotImplementedError
            return self._num_timesteps

        # Transform sample-wise to batch-wise spikecount limit.
        max_spikecount_norm = max_spikecount * self.batch_size

        x_accum = np.zeros_like(x)
        t = 0
        while True:
            x_accum += x
            # Neglect threshold here (always 1 in input layer)
            spikecount = np.sum(np.floor(x_accum))  # / v_thresh
            if spikecount > max_spikecount_norm:
                print(t)
                return min(t, self._num_timesteps)
            t += 1
