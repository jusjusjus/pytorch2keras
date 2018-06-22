"""
The Pytorch2Keras converter module over JIT-trace.
"""

import torch
import torch.jit
import torch.autograd
import torch.serialization
import contextlib
from torch.jit import _unique_state_dict

from .layers import AVAILABLE_CONVERTERS


@contextlib.contextmanager
def set_training(model, mode):
    """
    A context manager to temporarily set the training mode of 'model'
    to 'mode', resetting it when we exit the with-block.  A no-op if
    mode is None.
    """
    if mode is None:
        yield
        return
    old_mode = model.training
    if old_mode != mode:
        model.train(mode)
    try:
        yield
    finally:
        if old_mode != mode:
            model.train(old_mode)


def _optimize_graph(graph, aten):
    # run dce first to eliminate dead parts of the graph that might have been
    # left behind by things like symbolic_override
    torch._C._jit_pass_dce(graph)
    torch._C._jit_pass_lint(graph)

    torch._C._jit_pass_peephole(graph)
    torch._C._jit_pass_lint(graph)
    graph = torch._C._jit_pass_onnx(graph, aten)
    torch._C._jit_pass_lint(graph)
    torch._C._jit_pass_onnx_peephole(graph)
    torch._C._jit_pass_lint(graph)
    torch._C._jit_pass_dce(graph)
    torch._C._jit_pass_lint(graph)
    graph = torch._C._jit_pass_canonicalize(graph)
    torch._C._jit_pass_lint(graph)
    return graph


def get_node_id(node):
    import re
    node_id = re.search(r"[\d]+", node.__str__())
    return node_id.group(0)


def pytorch_to_keras(model, args, input_shape,
        change_ordering=False, training=False, verbose=False):
    """
    By given pytorch model convert layers with specified convertors.

    Args:
        model: pytorch model
        args: pytorch model arguments
        input_shape: keras input shape (using for InputLayer creation)
        change_ordering: change NCHW to NHWC
        training: switch model to training mode
        verbose: verbose output

    Returns:
        model: created keras model.
    """

    # PyTorch JIT tracing
    args = (args,) if isinstance(args, torch.autograd.Variable) else args

    orig_state_dict_keys = _unique_state_dict(model).keys()

    with set_training(model, training):
        trace, torch_out = torch.jit.get_trace_graph(model, args)

    if orig_state_dict_keys != _unique_state_dict(model).keys():
        raise RuntimeError("state_dict changed after running the tracer; "
                           "something weird is happening in your model!")

    # _optimize_trace(trace, False)
    trace.set_graph(_optimize_graph(trace.graph(), False))

    if verbose:
        print("trace.graph()")
        print(trace.graph())

    if verbose:
        print("trace.graph().outputs())")
        print(list(trace.graph().outputs()))

    # Get all graph nodes
    nodes = list(trace.graph().nodes())

    # Collect graph outputs
    graph_outputs = [n.uniqueName() for n in trace.graph().outputs()]
    print('Graph outputs:', graph_outputs)

    # Collect model state dict
    state_dict = _unique_state_dict(model)
    if verbose:
        print('State dict:', list(state_dict))

    import re
    import keras
    from keras import backend as K
    K.set_image_data_format('channels_first')

    layers = dict()
    layers['input'] = keras.layers.InputLayer(
        input_shape=input_shape, name='input'
    ).output

    outputs = []

    for node in nodes:
        node_inputs = list(node.inputs())
        node_input_names = []
        for node_input in node_inputs:
            if node_input.node().scopeName():
                node_input_names.append(get_node_id(node_input.node()))

        if len(node_input_names) == 0:
            node_input_names.append('input')

        node_type = node.kind()

        node_scope_name = node.scopeName()
        node_id = get_node_id(node)
        node_weights_name = '.'.join(
            re.findall(r'\[([\w\d.]+)\]', node_scope_name)
        )
        node_attrs = {k: node[k] for k in node.attributeNames()}

        node_outputs = list(node.outputs())
        node_outputs_names = []
        for node_output in node_outputs:
            if node_output.node().scopeName():
                node_outputs_names.append(node_output.node().scopeName())

        if verbose:
            print(' ____ ')
            print('graph node:', node_scope_name)
            print('type:', node_type)
            print('inputs:', node_input_names)
            print('outputs:', node_outputs_names)
            print('name in state_dict:', node_weights_name)
            print('attrs:', node_attrs)
            print('node_id:', node_id)
            print('is_terminal:', node_id in graph_outputs)
        AVAILABLE_CONVERTERS[node_type](
            params = node_attrs,
            w_name = node_weights_name,
            scope_name = node_id,
            inputs = node_input_names,
            layers = layers, weights=state_dict
        )
        if node_id in graph_outputs:
            outputs.append(layers[node_id])

    model = keras.models.Model(inputs=layers['input'], outputs=outputs)
    model.summary()

    if change_ordering:
        # Change from 'NCW' to 'NWC' ordering customary in tf
        import numpy as np
        config = model.get_config()
        output_shape = None
        for layer_type, lc in ((layer['class_name'], layer['config']) for layer in config['layers']):

            if 'batch_input_shape' in lc:
                if len(lc['batch_input_shape']) == 3:
                    N, C, W = lc['batch_input_shape']
                    lc['batch_input_shape'] = (N, W, C)
                elif len(lc['batch_input_shape']) == 4:
                    N, C, H, W = lc['batch_input_shape']
                    lc['batch_input_shape'] = (N, H, W, C)
                else:
                    raise NotImplementedError("len(batch_input_shape) should be either 3 or 4")
                output_shape = lc['batch_input_shape']

            if layer_type == 'Con1D':
                (N, W, _), K = output_shape, lc['kernel_size'][0]
                C = lc['filters']
                W -= K-1
                output_shape = (N, W, C)

            if 'target_shape' in lc:
                lc['target_shape'] = tuple(np.reshape(np.array([
                    list(lc['target_shape'][1:][:]),
                    lc['target_shape'][0]
                ]), -1))

            if 'data_format' in lc:
                lc['data_format'] = 'channels_last'

            if 'axis' in lc:
                lc['axis'] = len(output_shape)-1


        K.set_image_data_format('channels_last')

        # # For theano:
        # from keras.utils.layer_utils import convert_all_kernels_in_model
        # convert_all_kernels_in_model(model)

        # Set the weights into the model with new ordering
        # `Dense` layers after `Flatten` have their weights transposed.
        src_weights = []
        last_was_flatten = False
        last_shape = None
        for layer in model.layers:
            W = layer.get_weights()
            if last_was_flatten and W:
                assert len(last_shape) == 3, str(last_shape)
                A, b = W
                _, C, H = last_shape
                A.shape = (C, H, -1)
                A = np.ascontiguousarray(np.swapaxes(A, 0, 1))
                A.shape = (H*C, -1)
                W = [A, b]
                last_was_flatten = False
            if isinstance(layer, keras.layers.core.Flatten):
                last_was_flatten = True
            elif not last_was_flatten:
                last_shape = layer.output_shape
            src_weights.append(W)

        if K.backend() == 'tensorflow':
            # Tensorflow needs a new graph for the converted model
            # to retain the same scopes for the operators.
            import tensorflow as tf
            tf.reset_default_graph()
            K.set_session(tf.Session())
            model_tf_ordering = keras.models.Model.from_config(config)
            for dst, src in zip(model_tf_ordering.layers, src_weights):
                dst.set_weights(src)
        else:
            model_tf_ordering = keras.models.Model.from_config(config)
            for dst, src in zip(model_tf_ordering.layers, src_weights):
                dst.set_weights(src)

        model = model_tf_ordering

    return model
