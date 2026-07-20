# A clear visual walkthrough of how a neural network turns pixels into digit predictions — no buzzwords, just the math behind the layers.

1. Each neuron in a network is simply a container for a number between 0 and 1 called its activation; in a digit recognizer, the first layer has 784 neurons (one per pixel in a 28×28 image) and the last layer has 10 (one per digit).
2. A neuron in the next layer is activated by computing a weighted sum of the previous layer's activations, adding a bias, and then passing the result through a sigmoid function that squishes the value into the 0-to-1 range.
3. Weights encode which pixel pattern a neuron is detecting (e.g. positive weights on an edge region, negative on surrounding pixels) while the bias sets how high that weighted sum must be before the neuron becomes meaningfully active.
4. The entire network is just a function — about 13,000 weights and biases in this example — that takes 784 input numbers and outputs 10; learning means finding the specific numeric settings for all those parameters so the function produces correct predictions.
5. Modern networks mostly replaced the sigmoid with ReLU (rectified linear unit), which outputs zero below a threshold and the identity above it, because ReLU turned out to be far easier to train in deep networks.

**Speaker:** 3Blue1Brown (Grant Sanderson)
**Source:** https://www.youtube.com/watch?v=aircAruvnKk
