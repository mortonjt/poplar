#!/usr/bin/env python

# ----------------------------------------------------------------------------
# Copyright (c) 2019--, poplar development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file COPYING.txt, distributed with this software.
# ----------------------------------------------------------------------------
import re
import ast
from glob import glob
from setuptools import find_packages, setup

classes = """
    Development Status :: 2 - Pre-Alpha
    License :: OSI Approved :: BSD License
    Topic :: Software Development :: Libraries
    Topic :: Scientific/Engineering
    Topic :: Scientific/Engineering :: Bio-Informatics
    Programming Language :: Python :: 3
    Programming Language :: Python :: 3 :: Only
    Operating System :: Unix
    Operating System :: POSIX
    Operating System :: MacOS :: MacOS X
"""
classifiers = [s.strip() for s in classes.split('\n') if s]

description = ('Microbe-metabolite interactions through neural networks')

with open('README.md') as f:
    long_description = f.read()

# version parsing from __init__ pulled from Flask's setup.py
# https://github.com/mitsuhiko/flask/blob/master/setup.py
_version_re = re.compile(r'__version__\s+=\s+(.*)')
with open('poplar/__init__.py', 'rb') as f:
    hit = _version_re.search(f.read().decode('utf-8')).group(1)
    version = str(ast.literal_eval(hit))


setup(name='poplar',
      version=version,
      license='BSD-3-Clause',
      description=description,
      long_description=long_description,
      long_description_content_type='text/markdown',
      author="poplar development team",
      author_email="jamietmorton@gmail.com",
      maintainer="poplar development team",
      maintainer_email="jamietmorton@gmail.com",
      packages=find_packages(),
      scripts=glob('scripts/poplar'),
      install_requires=[
          'biopython',
          'numpy >= 1.9.2',
          'pandas >= 0.18.0',
          'scipy >= 0.15.1',
          'nose >= 1.3.7',
          'fairseq',
          'transformers',
          'tqdm',
          'torch'
      ],
      classifiers=classifiers,
      zip_safe=False)
