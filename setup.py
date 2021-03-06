from setuptools import setup, PEP420PackageFinder


setup(
    name='tangled.web',
    version='1.0a13.dev0',
    description='RESTful Web Framework',
    long_description=open('README.rst').read(),
    url='https://tangledframework.org/',
    download_url='https://github.com/TangledWeb/tangled.web/tags',
    author='Wyatt Baldwin',
    author_email='self@wyattbaldwin.com',
    packages=PEP420PackageFinder.find(include=['tangled*']),
    include_package_data=True,
    install_requires=[
        'tangled>=1.0a12',
        'MarkupSafe>=0.23',
        'WebOb>=1.5.1',
        'WebTest>=2.0.29',
    ],
    entry_points="""
    [tangled.scripts]
    serve = tangled.web.scripts.serve
    show = tangled.web.scripts.show

    [tangled.scaffolds]
    basic = tangled.web.scaffolds:basic

    """,
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Environment :: Web Environment',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
    ],
)
