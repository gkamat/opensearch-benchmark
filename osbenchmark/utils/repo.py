# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.
# Modifications Copyright OpenSearch Contributors. See
# GitHub history for details.
# Licensed to Elasticsearch B.V. under one or more contributor
# license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright
# ownership. Elasticsearch B.V. licenses this file to you under
# the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#	http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import logging
import os
import sys

from osbenchmark import exceptions, config
from osbenchmark.utils import io, git, console, versions


class BenchmarkRepository:
    """
    Manages OSB resources (e.g. provision_configs or workloads).
    """

    default = "default-provision-config"

    def __init__(self, default_directory, root_dir, repo_name, resource_name, offline, fetch=True):
        # If no URL is found, we consider this a local only repo (but still require that it is a git repo)
        self.directory = default_directory
        self.remote = self.directory is not None and self.directory != BenchmarkRepository.default and self.directory.strip() != ""
        self.repo_dir = os.path.join(root_dir, repo_name)
        self.resource_name = resource_name
        self.offline = offline
        self.logger = logging.getLogger(__name__)
        self.revision = None

        if self.remote and not self.offline and fetch:
            # a normal git repo with a remote
            if not git.is_working_copy(self.repo_dir):
                git.clone(src=self.repo_dir, remote=self.directory)
            else:
                try:
                    git.fetch(src=self.repo_dir)
                except exceptions.SupplyError:
                    console.warn("Could not update %s. Continuing with your locally available state." % self.resource_name)
        else:
            if not git.is_working_copy(self.repo_dir) and repo_name != "default":
                if io.exists(self.repo_dir):
                    raise exceptions.SystemSetupError("[{src}] must be a git repository.\n\nPlease run:\ngit -C {src} init"
                                                      .format(src=self.repo_dir))

    def update(self, distribution_version):
        try:
            if self.remote:
                branch = versions.best_match(git.branches(self.repo_dir, remote=self.remote), distribution_version)
                if branch:
                    # Allow uncommitted changes iff we do not have to change the branch
                    self.logger.info(
                        "Checking out [%s] in [%s] for distribution version [%s].", branch, self.repo_dir, distribution_version)
                    git.checkout(self.repo_dir, branch=branch)
                    self.logger.info("Rebasing on [%s] in [%s] for distribution version [%s].", branch, self.repo_dir, distribution_version)
                    try:
                        git.rebase(self.repo_dir, branch=branch)
                        self.revision = git.head_revision(self.repo_dir)
                    except exceptions.SupplyError:
                        self.logger.exception("Cannot rebase due to local changes in [%s]", self.repo_dir)
                        console.warn(
                            "Local changes in [%s] prevent %s update from remote. Please commit your changes." %
                            (self.repo_dir, self.resource_name))
                    return
                else:
                    msg = "Could not find %s remotely for distribution version [%s]. Trying to find %s locally." % \
                          (self.resource_name, distribution_version, self.resource_name)
                    self.logger.warning(msg)
            branch = versions.best_match(git.branches(self.repo_dir, remote=False), distribution_version)
            if branch:
                if git.current_branch(self.repo_dir) != branch:
                    self.logger.info("Checking out [%s] in [%s] for distribution version [%s].",
                                     branch, self.repo_dir, distribution_version)
                    git.checkout(self.repo_dir, branch=branch)
                    self.revision = git.head_revision(self.repo_dir)
            else:
                self.logger.info("No local branch found for distribution version [%s] in [%s]. Checking tags.",
                                 distribution_version, self.repo_dir)
                tag = self._find_matching_tag(distribution_version)
                if tag:
                    self.logger.info("Checking out tag [%s] in [%s] for distribution version [%s].",
                                     tag, self.repo_dir, distribution_version)
                    git.checkout(self.repo_dir, branch=tag)
                    self.revision = git.head_revision(self.repo_dir)
                else:
                    raise exceptions.SystemSetupError("Cannot find %s for distribution version %s"
                                                      % (self.resource_name, distribution_version))
        except exceptions.SupplyError as e:
            tb = sys.exc_info()[2]
            raise exceptions.DataError("Cannot update %s in [%s] (%s)." % (self.resource_name, self.repo_dir, e.message)).with_traceback(tb)

    def _find_matching_tag(self, distribution_version):
        tags = git.tags(self.repo_dir)
        for version in versions.variants_of(distribution_version):
            # tags have a "v" prefix by convention.
            tag_candidate = "v{}".format(version)
            if tag_candidate in tags:
                return tag_candidate
        return None

    def checkout(self, revision):
        self.logger.info("Checking out revision [%s] in [%s].", revision, self.repo_dir)
        git.checkout(self.repo_dir, revision)

    def set_provision_configs_dir(self, repo_revision, distribution_version, cfg):
        if self.directory == BenchmarkRepository.default:
            self._use_default_provision_configs_dir(distribution_version, cfg)
        elif repo_revision:
            self.checkout(repo_revision)
        else:
            self.update(distribution_version)
            cfg.add(config.Scope.applicationOverride, "builder", "repository.revision", self.revision)

        return cfg

    def _use_default_provision_configs_dir(self, distribution_version, cfg):
        self.logger.info("Using default-provision-config directory within repository")
        root_dir = cfg.opts("node", "benchmark.root")
        provision_configs_path = os.path.join(root_dir, "resources/provision_configs")

        branch_version = self._select_branch_version(distribution_version, provision_configs_path)

        # Include selected branch
        self.repo_dir = os.path.join(provision_configs_path, branch_version)

    def _select_branch_version(self, distribution_version, pc_path):
        # Branches have been moved into resources/provision_configs
        branches = [b for b in os.listdir(pc_path) if os.path.isdir(os.path.join(pc_path, b)) and b != "main"]
        branches.sort(key=lambda b: list(map(int, b.split('.'))), reverse=True)
        self.logger.info("branches: %s", branches)
        convert = lambda s: list(map(int, s.split('.')))
        if distribution_version is not None:
            # Return a branch that is less than or equal to the distribution version
            for branch in branches:
                if convert(distribution_version) >= convert(branch):
                    return branch

            raise Exception("Distribution version is less than available ES versions for provision-configs.")

        # Distribution version is Nonetype if not specified in command line
        return "main"
