from setuptools import setup, find_packages

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

# get version from __version__ variable in bioprime/__init__.py
from bioprime import __version__ as version

setup(
	name="bioprime",
	version=version,
	description="bioprime",
	author="IBSL",
	author_email="design@indibasolutions.com",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
