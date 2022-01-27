import torch
from torch import nn
from typing import Iterable, Callable, Optional, List
from torch.distributions import Distribution, Normal, Independent


from pyro.distributions import transforms
from pyro.nn import AutoRegressiveNN, DenseNN

from sbi.types import Shape, TorchTransform

from .vi_utils import get_modules, get_parameters, filter_kwrags_for_func
from .vi_utils import docstring_parameter

# Supported transforms and flows are registered here i.e. associated with a name

_TRANSFORMS = {}
_TRANSFORMS_INITS = {}
_FLOW_BUILDERS = {}


def register_transform(
    cls: Optional[object] = None,
    name: Optional[str] = None,
    inits: Callable = lambda *args, **kwargs: (args, kwargs),
):
    """Decorator to register a learnable transformation.


    Args:
        cls: Class to register
        name: Name of the transform.
        inits: Function that provides initial args and kwargs.


    """

    def _register(cls):
        if name is None:
            cls_name = cls.__name__
        else:
            cls_name = name
        if cls_name in _TRANSFORMS:
            raise ValueError(f"The transform {cls_name} is already registered")
        else:
            _TRANSFORMS[cls_name] = cls
            _TRANSFORMS_INITS[cls_name] = inits
        return cls

    if cls is None:
        return _register
    else:
        return _register(cls)


def get_all_transforms() -> List[str]:
    """Returns all registered transforms.

    Returns:
        List[str]: List of names of all transforms.

    """
    return list(_TRANSFORMS.keys())


@docstring_parameter(get_all_transforms())
def get_transform(name: str, dim, **kwargs):
    """Returns an initialized transformation



    Args:
        name: Name of the transform, must be one of {0}
        dim: Input dimension.
        kwargs: All associated parameters which will be passed through.

    Returns:
        Transform: Invertible transformation.

    """
    name = name.lower()
    transform = _TRANSFORMS[name]
    overwritable_kwargs = filter_kwrags_for_func(transform.__init__, kwargs)
    args, default_kwargs = _TRANSFORMS_INITS[name](dim, **kwargs)
    kwargs = {**default_kwargs, **overwritable_kwargs}
    return _TRANSFORMS[name](*args, **kwargs)


def register_flow_builder(cls=None, name=None):
    """Registers a function that builds a normalizing flow.

    Args:
        cls: Builder that is registered.
        name: Name of the builder.


    """

    def _register(cls):
        if name is None:
            cls_name = cls.__name__
        else:
            cls_name = name
        if cls_name in _FLOW_BUILDERS:
            raise ValueError(f"The flow {cls_name} is not registered as default.")
        else:
            _FLOW_BUILDERS[cls_name] = cls
        return cls

    if cls is None:
        return _register
    else:
        return _register(cls)


def get_default_flows() -> List[str]:
    """Returns names of all registered flow builders.

    Returns:
        List[str]: List of names.

    """
    return list(_FLOW_BUILDERS.keys())


def get_flow_builder(name, event_shape, link_flow, **kwargs) -> Distribution:
    """Returns an normalizing flow, by instantiating the flow build with all arguments.

    Args:
        name: Name of the flow.
        event_shape: Event shape.
        link_flow: Transform that maps to the prior support.

    Returns:
        Distribution: Builded trainable distribution.

    """
    builder = _FLOW_BUILDERS[name]
    return builder(event_shape, link_flow, **kwargs)


# Initialization functions.


def init_affine_autoregressive(dim, **kwargs):
    """Provides the default initial arguments for an affine autoregressive transform."""
    hidden_dims = kwargs.pop("hidden_dims", [5 * dim + 5])
    skip_connections = kwargs.pop("skip_connections", False)
    nonlinearity = kwargs.pop("nonlinearity", nn.ReLU())
    arn = AutoRegressiveNN(
        dim, hidden_dims, nonlinearity=nonlinearity, skip_connections=skip_connections
    )
    return [arn], {"log_scale_min_clip": -3.0}


def init_spline_autoregressive(dim, **kwargs):
    """Provides the default initial arguments for an spline autoregressive transform."""
    hidden_dims = kwargs.pop("hidden_dims", [5 * dim + 5])
    skip_connections = kwargs.pop("skip_connections", False)
    nonlinearity = kwargs.pop("nonlinearity", nn.ReLU())
    count_bins = kwargs.get("count_bins", 10)
    order = kwargs.get("order", "linear")
    bound = kwargs.get("bound", 5)
    if order == "linear":
        param_dims = [count_bins, count_bins, (count_bins - 1), count_bins]
    else:
        param_dims = [count_bins, count_bins, (count_bins - 1)]
    neural_net = AutoRegressiveNN(
        dim,
        hidden_dims,
        param_dims=param_dims,
        skip_connections=skip_connections,
        nonlinearity=nonlinearity,
    )
    return [dim, neural_net], {"count_bins": count_bins, "bound": bound, "order": order}


def init_affine_coupling(dim, **kwargs):
    """Provides the default initial arguments for an affine autoregressive transform."""
    assert dim > 1, "In 1d this would be equivalent to affine flows, use them!"
    nonlinearity = kwargs.pop("nonlinearity", nn.ReLU())
    split_dim = kwargs.get("split_dim", dim // 2)
    hidden_dims = kwargs.pop("hidden_dims", [5 * dim + 5, 5 * dim + 5])
    arn = DenseNN(split_dim, hidden_dims, nonlinearity=nonlinearity)
    return [split_dim, arn], {"log_scale_min_clip": -3.0}


def init_spline_coupling(dim, **kwargs):
    """Intitialize a spline coupling transform, by providing necessary args and kwargs."""
    assert dim > 1, "In 1d this would be equivalent to affine flows, use them!"
    split_dim = kwargs.get("split_dim", dim // 2)
    hidden_dims = kwargs.pop("hidden_dims", [5 * dim + 5, 5 * dim + 5])
    nonlinearity = kwargs.pop("nonlinearity", nn.ReLU())
    count_bins = kwargs.get("count_bins", 10)
    order = kwargs.get("order", "linear")
    bound = kwargs.get("bound", 5)
    if order == "linear":
        param_dims = [
            (dim - split_dim) * count_bins,
            (dim - split_dim) * count_bins,
            (dim - split_dim) * (count_bins - 1),
            (dim - split_dim) * count_bins,
        ]
    else:
        param_dims = [
            (dim - split_dim) * count_bins,
            (dim - split_dim) * count_bins,
            (dim - split_dim) * (count_bins - 1),
        ]
    neural_net = DenseNN(split_dim, hidden_dims, param_dims, nonlinearity=nonlinearity)
    return [dim, split_dim, neural_net], {
        "count_bins": count_bins,
        "bound": bound,
        "order": order,
    }


# Register these directly from pyro

register_transform(
    transforms.AffineAutoregressive,
    "affine_autoregressive",
    inits=init_affine_autoregressive,
)
register_transform(
    transforms.SplineAutoregressive,
    "spline_autoregressive",
    inits=init_spline_autoregressive,
)

register_transform(
    transforms.AffineCoupling, "affine_coupling", inits=init_affine_coupling
)

register_transform(
    transforms.SplineCoupling, "spline_coupling", inits=init_spline_coupling
)


# Register these very simple transforms.


@register_transform(
    name="affine_diag",
    inits=lambda dim, **kwargs: (
        [],
        {"loc": torch.zeros(dim), "scale": torch.ones(dim)},
    ),
)
class AffineTransform(transforms.AffineTransform):
    """Trainable version of an Affine transform. This can be used to get diagonal
    Gaussian approximations."""

    __doc__ += transforms.AffineTransform.__doc__

    def parameters(self):
        self.loc.requires_grad_(True)
        self.scale.requires_grad_(True)
        yield self.loc
        yield self.scale

    def with_cache(self, cache_size=1):
        if self._cache_size == cache_size:
            return self
        return AffineTransform(self.loc, self.scale, cache_size=cache_size)

    def log_abs_jacobian_diag(self, x, y):
        return self.scale


@register_transform(
    name="affine_tril",
    inits=lambda dim, **kwargs: (
        [],
        {"loc": torch.zeros(dim), "scale_tril": torch.eye(dim)},
    ),
)
class LowerCholeskyAffine(transforms.LowerCholeskyAffine):
    """Trainable version of a Lower Cholesky Affine transform. This can be used to get
    full Gaussian approximations."""

    __doc__ += transforms.LowerCholeskyAffine.__doc__

    def parameters(self):
        self.loc.requires_grad_(True)
        self.scale_tril.requires_grad_(True)
        yield self.loc
        yield self.scale_tril

    def with_cache(self, cache_size=1):
        if self._cache_size == cache_size:
            return self
        return LowerCholeskyAffine(self.loc, self.scale_tril, cache_size=cache_size)

    def log_abs_det_jacobian(self, x, y):
        """This modification allows batched scale_tril matrices."""
        return self.log_abs_jacobian_diag(x, y).sum(-1)

    def log_abs_jacobian_diag(self, x, y):
        """This returns the full diagonal which is necessary to compute conditionals"""
        dim = self.scale_tril.dim()
        return torch.diagonal(self.scale_tril, dim1=dim - 2, dim2=dim - 1).log()


# Now build Normalizing flows


class TransformedDistribution(torch.distributions.TransformedDistribution):
    """This is TransformedDistribution with the capability to return parameters!"""

    __doc__ += torch.distributions.TransformedDistribution.__doc__

    def parameters(self):
        for t in self.transforms:
            yield from get_parameters(t)

    def modules(self):
        for t in self.transforms:
            yield from get_modules(t)


@docstring_parameter(list(_TRANSFORMS.keys()))
def build_flow(
    event_shape: torch.Size,
    link_flow: transforms.Transform,
    num_flows: int = 5,
    transform: str = "affine_autoregressive",
    permute: bool = True,
    batch_norm: bool = False,
    base_dist: Distribution = None,
    **kwargs,
) -> TransformedDistribution:
    """Generates a Transformed Distribution where the base_dist is transformed by
       num_flows bijective transforms of specified type.



    Args:
        event_shape: Shape of the events generated by the distribution.
        link_flow: Links to a specific support .
        num_flows: Number of normalizing flows that are concatenated.
        transform: The type of normalizing flow. Should be one of {0}
        permute: Permute dimension after each layer. This may helpfull for
            autoregressive or coupling nets.
        batch_norm: Perform batch normalization.
        base_dist: Base distribution. If 'None' then a standard Gaussian is used.
        kwargs: Hyperparameters are added here.
    Returns:
        TransformedDistribution

    """
    # Some transforms increase dimension by decreasing the degrees of freedom e.g.
    # SoftMax.
    additional_dim = len(link_flow(torch.zeros(event_shape))) - torch.tensor(
        event_shape
    )
    event_shape = torch.Size(torch.tensor(event_shape) - additional_dim)
    # Base distribution is standard normal if not specified
    if base_dist is None:
        base_dist = Independent(
            Normal(torch.zeros(event_shape), torch.ones(event_shape)),
            1,
        )
    # Generate normalizing flow
    if isinstance(event_shape, int):
        dim = event_shape
    elif isinstance(event_shape, Iterable):
        dim = event_shape[-1]
    else:
        raise ValueError("The eventshape must either be an Integer or a Iterable.")

    flows = []
    for i in range(num_flows):
        flows.append(get_transform(transform, dim, **kwargs).with_cache())
        if permute and i < num_flows - 1:
            flows.append(transforms.permute(dim).with_cache())
        if batch_norm and i < num_flows - 1:
            flows.append(transforms.batchnorm(dim))
    flows.append(link_flow.with_cache())
    dist = TransformedDistribution(base_dist, flows)
    return dist


@docstring_parameter(list(_TRANSFORMS.keys()))
@register_flow_builder(name="gaussian_diag")
def gaussian_diag_flow_builder(event_shape, link_flow, **kwargs):
    """Generates a Gaussian distribution with diagonal covariance.

    Args:
        event_shape: Shape of the events generated by the distribution.
        link_flow: Links to a specific support .
        kwargs: Hyperparameters are added here.
            loc: Initial location.
            scale: Initial triangular matrix.

    Returns:
        TransformedDistribution

    """
    if "transform" in kwargs:
        kwargs.pop("transform")
    if "base_dist" in kwargs:
        kwargs.pop("base_dist")
    if "num_flows" in kwargs:
        kwargs.pop("num_flows")
    return build_flow(
        event_shape,
        link_flow,
        transform="affine_diag",
        num_flows=1,
        shuffle=False,
        **kwargs,
    )


@register_flow_builder(name="gaussian")
def gaussian_flow_builder(
    event_shape: Shape, link_flow: TorchTransform, **kwargs
) -> TransformedDistribution:
    """Generates a Gaussian distribution.

    Args:
        event_shape: Shape of the events generated by the distribution.
        link_flow: Links to a specific support .
        kwargs: Hyperparameters are added here.
            loc: Initial location.
            scale_tril: Initial triangular matrix.

    Returns:
        TransformedDistribution

    """
    if "transform" in kwargs:
        kwargs.pop("transform")
    if "base_dist" in kwargs:
        kwargs.pop("base_dist")
    if "num_flows" in kwargs:
        kwargs.pop("num_flows")
    return build_flow(
        event_shape,
        link_flow,
        transform="affine_tril",
        shuffle=False,
        num_flows=1,
        **kwargs,
    )


@register_flow_builder(name="maf")
def masked_autoregressive_flow_builder(
    event_shape: Shape, link_flow: TorchTransform, **kwargs
) -> TransformedDistribution:
    """Generates a masked autoregressive flow

    Args:
        event_shape: Shape of the events generated by the distribution.
        link_flow: Links to a specific support.
        num_flows: Number of normalizing flows that are concatenated.
        permute: Permute dimension after each layer. This may helpfull for
            autoregressive or coupling nets.
        batch_norm: Perform batch normalization.
        base_dist: Base distribution. If 'None' then a standard Gaussian is used.
        kwargs: Hyperparameters are added here.
            hidden_dims: The dimensionality of the hidden units per layer.
            skip_connections: Whether to add skip connections from the input to the
                output.
            nonlinearity: The nonlinearity to use in the feedforward network such as
                torch.nn.ReLU().
            log_scale_min_clip: The minimum value for clipping the log(scale) from
                the autoregressive NN
            log_scale_max_clip: The maximum value for clipping the log(scale) from
                the autoregressive NN
            sigmoid_bias: A term to add the logit of the input when using the stable
                tranform.
            stable: When true, uses the alternative "stable" version of the transform.
                Yet this version is also less expressive.

    Returns:
        TransformedDistribution

    """
    if "transform" in kwargs:
        kwargs.pop("transform")
    return build_flow(
        event_shape, link_flow, transform="affine_autoregressive", **kwargs
    )


@register_flow_builder(name="nsf")
def spline_autoregressive_flow_builder(
    event_shape: Shape, link_flow: TorchTransform, **kwargs
) -> TransformedDistribution:
    """Generates an autoregressive neural spline flow.

    Args:
        event_shape: Shape of the events generated by the distribution.
        link_flow: Links to a specific support .
        num_flows: Number of normalizing flows that are concatenated.
        permute: Permute dimension after each layer. This may helpfull for
            autoregressive or coupling nets.
        batch_norm: Perform batch normalization.
        base_dist: Base distribution. If 'None' then a standard Gaussian is used.
        kwargs: Hyperparameters are added here.
            hidden_dims: The dimensionality of the hidden units per layer.
            skip_connections: Whether to add skip connections from the input to the
                output.
            nonlinearity: The nonlinearity to use in the feedforward network such as
                torch.nn.ReLU().
            count_bins: The number of segments comprising the spline.
            bound: The quantity `K` determining the bounding box.
            order: One of ['linear', 'quadratic'] specifying the order of the spline.

    Returns:
        TransformedDistribution

    """
    if "transform" in kwargs:
        kwargs.pop("transform")
    return build_flow(
        event_shape, link_flow, transform="spline_autoregressive", **kwargs
    )


@register_flow_builder(name="mcf")
def coupling_flow_builder(
    event_shape: Shape, link_flow: TorchTransform, **kwargs
) -> TransformedDistribution:
    """Generates a affine coupling flow.

    Args:
        event_shape: Shape of the events generated by the distribution.
        link_flow: Links to a specific support.
        num_flows: Number of normalizing flows that are concatenated.
        permute: Permute dimension after each layer. This may helpfull for
            autoregressive or coupling nets.
        batch_norm: Perform batch normalization.
        base_dist: Base distribution. If 'None' then a standard Gaussian is used.
        kwargs: Hyperparameters are added here.
            hidden_dims: The dimensionality of the hidden units per layer.
            skip_connections: Whether to add skip connections from the input to the
                output.
            nonlinearity: The nonlinearity to use in the feedforward network such as
                torch.nn.ReLU().
            log_scale_min_clip: The minimum value for clipping the log(scale) from
                the autoregressive NN
            log_scale_max_clip: The maximum value for clipping the log(scale) from
                the autoregressive NN
            split_dim : The dimension to split the input on for the coupling transform.

    Returns:
        TransformedDistribution

    """
    if "transform" in kwargs:
        kwargs.pop("transform")
    return build_flow(event_shape, link_flow, transform="affine_coupling", **kwargs)


@register_flow_builder(name="scf")
def spline_coupling_flow_builder(
    event_shape: Shape, link_flow: TorchTransform, **kwargs
) -> TransformedDistribution:
    """Generates an spline coupling flow.

    Args:
        event_shape: Shape of the events generated by the distribution.
        link_flow: Links to a specific support .
        num_flows: Number of normalizing flows that are concatenated.
        permute: Permute dimension after each layer. This may helpfull for
            autoregressive or coupling nets.
        batch_norm: Perform batch normalization.
        base_dist: Base distribution. If 'None' then a standard Gaussian is used.
        kwargs: Hyperparameters are added here.
            hidden_dims: The dimensionality of the hidden units per layer.
            nonlinearity: The nonlinearity to use in the feedforward network such as
                torch.nn.ReLU().
            count_bins: The number of segments comprising the spline.
            bound: The quantity `K` determining the bounding box.
            order: One of ['linear', 'quadratic'] specifying the order of the spline.
            split_dim : The dimension to split the input on for the coupling transform.

    Returns:
        TransformedDistribution

    """
    if "transform" in kwargs:
        kwargs.pop("transform")
    return build_flow(event_shape, link_flow, transform="spline_coupling", **kwargs)
