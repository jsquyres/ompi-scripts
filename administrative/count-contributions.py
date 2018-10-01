#!/usr/bin/env python3
#
# Non-python core dependencies:
#
# * pygit2: https://pypi.org/project/pygit2/
#   pip install pygit2
# * Matplotlib: https://matplotlib.org/
#   pip install matplotlib
#
#############################################3
#
# Discussion with Ralph:
#
# - Show statistics from "common" code:
#   - OPAL without BTLs
#   - ORTE
#   - OMPI without MTLs and vendor PMLs
#   - OSHMEM
#

import matplotlib.pyplot as pyplot
import datetime
import argparse
import logging
import pygit2
import csv
import os
import re

from pprint import pformat
from pprint import pprint
from pygit2 import GIT_SORT_TOPOLOGICAL, GIT_SORT_REVERSE

##########################################################################

def setup_cli():
    parser = argparse.ArgumentParser(description='Analyze Open MPI contributions')
    parser.add_argument('--repo',
                        required=True,
                        help='Directory for git repo')
    parser.add_argument('--remote',
                        default='origin',
                        help='Which remote to use')

    parser.add_argument('--skip-merge-commits',
                        default=True,
                        action='store_true',
                        help='When specified, skip merge commits in the statistics')

    parser.add_argument('--days',
                        default='365',
                        type=int,
                        help='Number of days to examine')

    parser.add_argument('--debug',
                        action='store_true',
                        help='Enable extra output for debugging')

    args = parser.parse_args()

    if not os.path.exists(args.repo):
        print("ERROR: Repo directory does not exist ({r})"
              .format(r=args.repo))
        exit(1)
    if not os.path.isdir(args.repo):
        print("ERROR: Repo is not a directory ({r})"
              .format(r=args.repo))
        exit(1)

    return args

def setup_logging(args):
    log = logging.getLogger('GithubPRwaiter')
    level = logging.INFO
    if args.debug:
        level = logging.DEBUG
    log.setLevel(level)

    ch = logging.StreamHandler()
    ch.setLevel(level)

    format = '%(asctime)s %(levelname)s: %(message)s'
    formatter = logging.Formatter(format)

    ch.setFormatter(formatter)

    log.addHandler(ch)

    return log

##########################################################################

def read_mailmap(repo_dir, log):
    mailmap = dict()

    filename = os.path.join(repo_dir, '.mailmap')
    log.debug("Checking for mailmap: {f}".format(f=filename))
    if not os.path.exists(filename):
        return mailmap

    comment_re = re.compile('^(.*)(#.*)$')
    parse_re   = re.compile('^(.*)<(.+@.+)>\s+<(.+@.+)>$')

    with open(filename, 'r') as mmfp:
        for line in mmfp:
            line = line.strip()

            # Strip off comments
            ret = comment_re.search(line)
            if ret:
                line = ret.group(1)

            # Skip empty lines
            if len(line) == 0:
                continue

            log.debug("Checking line: {l}".format(l=line))
            ret         = parse_re.search(line)
            # Catch / skip bogus email addresses
            if not ret:
                continue
            name        = ret.group(1)
            real_email  = ret.group(2)
            alias_email = ret.group(3)

            if real_email not in mailmap:
                mailmap[real_email] = {
                    'name'    : name,
                    'real'    : real_email,
                    'aliases' : list(),
                }

            mailmap[real_email]['aliases'].append(alias_email)

    return mailmap

##########################################################################

def shorthash(commit, len=10):
    sha = commit.id.__str__()
    return sha[0:len]

def find_remote(repo, desired_remote, log):
    log.info("Finding desired remote...")

    remotes = list(repo.remotes)
    for remote in remotes:
        if remote.name == desired_remote:
            log.info("Using remote: {remote}".format(remote=remote.name))
            return remote

    log.error("Remote '{remote}' not found"
              .format(remote=desired_remote))
    exit(1)

def find_branches(repo, remote, desired_branches, log):
    log.info("Finding desired branches in remote {remote}..."
             .format(remote=remote.name))

    if desired_branches:
        desired_branches = desired_branches.split(',')

    branches = list()
    for branch_name in repo.branches.remote:
        want = False

        parts = branch_name.split("/", maxsplit=2)
        remote_name = parts[0]
        branch_name = parts[1]

        if remote_name != remote.name:
            want = False
            reason = 'wrong remote'

        elif branch_name == "HEAD":
            want = False

        elif not desired_branches:
            want = True

        elif branch_name in desired_branches:
            want = True

        else:
            want = False
            reason = 'not desired branch'

        if not want:
            log.debug("Skipping {remote}/{branch} ({reason})"
                      .format(remote=remote_name,
                              branch=branch_name,
                              reason=reason))
            continue

        full_name = ('{remote}/{branch}'
                     .format(remote=remote_name,
                             branch=branch_name))
        log.info("Found branch: {full}".format(full=full_name))

        branches.append(repo.branches[full_name])

    return branches

##########################################################################

def examine_branch(repo, branch, start_timestamp, commits, skip_merges, log):
    log.info("Finding relevant commits on branch {b}..."
             .format(b=branch.branch_name))

    ref_name = "refs/remotes/" + branch.branch_name
    ref      = repo.references.get(ref_name)
    commit   = ref.get_object()
    sha      = shorthash(commit)

    unique               = dict()
    unique['inside']     = 0
    unique['outside']    = 0

    duplicate            = dict()
    duplicate['inside']  = 0
    duplicate['outside'] = 0

    counts               = dict()
    counts['total']      = 0
    counts['unique']     = unique
    counts['duplicate']  = duplicate

    for commit in repo.walk(commit.id,
                            GIT_SORT_TOPOLOGICAL | GIT_SORT_REVERSE):
        sha = shorthash(commit)

        # If this is a merge commit and we're skipping merge commits,
        # then discard this commit.
        if len(commit.parents) > 1 and skip_merges:
            log.debug("Skipping merge commit {sha}"
                      .format(sha=sha))
            continue

        # This is a commit we want to count.
        counts['total'] = counts['total'] + 1

        key1  = None
        key2  = None
        happy = 0

        if sha in commits:
            key1  = 'duplicate'
        else:
            key1  = 'unique'
            happy = happy + 1

        t = commit.commit_time
        dt = datetime.datetime.fromtimestamp(t)
        if dt < start_timestamp:
            key2  = 'outside'
        else:
            key2  = 'inside'
            happy = happy + 1

        counts[key1][key2] = counts[key1][key2] + 1

        # If this is both a unique commit and is inside the date
        # range, save it.
        if happy == 2:
            # Compute the diff.  This step is computationally
            # expensive, so only do it for commits that we're saving.
            if len(commit.parents) > 0:
                diff = commit.tree.diff_to_tree(commit.parents[0].tree)
            else:
                log.debug("Found commit with 0 parents")
                diff = commit.tree.diff_to_tree()

            commits[sha] = {
                'sha'    : sha,
                'commit' : commit,
                'branch' : branch,
                'diff'   : diff,
            }

        log.debug("Commit {sha}: {direction} ({t})"
                  .format(sha=sha, direction=key2, t=dt))

    log.info("Statistics for {ref}:".format(ref=ref_name))
    log.info(pformat(counts))

    return commits

##########################################################################

def examine_contributions(repo, commits, mailman, log):
    log.info("Analyzing commits from all the branches...")

    committers = dict()
    domains    = dict()

    for sha, data in commits.items():
        diff   = data['diff']
        commit = data['commit']
        name   = commit.author.name
        email  = commit.author.email
        parts  = email.split('@')
        # Catch / skip bogus email addresses
        if len(parts) < 2:
            continue
        domain = parts[1]

        # Take only the last 2 parts of the domain
        parts = domain.split('.')
        if len(parts) > 2:
            domain = '.'.join(parts[-2:])

        # Log the committer
        if email not in committers:
            committers[email] = {
                'name'          : name,
                'email'         : email,
                'domain'        : domain,
                'num_commits'   : 0,
                'num_additions' : 0,
                'num_deletions' : 0,
            }

        # NOTE: diff.stats.[insertions|deletions] are opposite what
        # you think they are:
        # stats.insertions: lines deleted in a diff
        # stats.deletions:  lines added in a diff
        committers[email]['num_commits']   = committers[email]['num_commits'] + 1
        committers[email]['num_additions'] = committers[email]['num_additions'] + diff.stats.deletions
        committers[email]['num_deletions'] = committers[email]['num_deletions'] + diff.stats.insertions

        # Log the domain
        if domain not in domains:
            domains[domain] = {
                'domain'        : domain,
                'num_commits'   : 0,
                'num_additions' : 0,
                'num_deletions' : 0,
            }
        domains[domain]['num_commits']   = domains[domain]['num_commits'] + 1
        domains[domain]['num_additions'] = domains[domain]['num_additions'] + diff.stats.deletions
        domains[domain]['num_deletions'] = domains[domain]['num_deletions'] + diff.stats.insertions

    # Debug/sanity check
    i = 0
    for email, data in committers.items():
        i = i + data['num_commits']
    if i != len(commits):
        print("ODDITY: total number of commits ({n}) != sum of committers commits ({i})"
              .format(n=len(commits), i=i))
    else:
        log.debug("Pass committers' count sanity check")

    i = 0
    for domain, data in domains.items():
        i = i + data['num_commits']
    if i != len(commits):
        print("ODDITY: total number of commits ({n}) != sum of domains commits ({i})"
              .format(n=len(commits), i=i))
    else:
        log.debug("Pass domains count sanity check")

    return committers, domains

##########################################################################

def write_commits_csv(commits, log):
    values = dict()
    for sha, data in commits.items():
        diff   = data['diff']
        branch = data['branch']
        commit = data['commit']
        t      = commit.commit_time
        dt     = datetime.datetime.fromtimestamp(t)

        values[sha] = {
            'sha'           : sha,
            'name'          : commit.author.name,
            'email'         : commit.author.email,
            'date'          : dt,
            'branch'        : branch.branch_name,
            'num_additions' : diff.stats.deletions,
            'num_deletions' : diff.stats.insertions,
            'url'           : 'https://github.com/open-mpi/ompi/commit/' + sha,
        }

    write_csv(values, 'commits.csv', log)

def write_csv(data, filename, log):
    log.info("Writing {f}...".format(f=filename))

    for key in data:
        first_key = key
        break
    log.debug("Got first key: {k}".format(k=first_key))
    fieldnames = sorted(data[first_key].keys())
    log.debug("Got fieldnames: {f}".format(f=fieldnames))

    with open(filename, 'w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames,
                                quoting=csv.QUOTE_ALL)
        writer.writeheader()

        for d in data:
            row = dict()
            for f in fieldnames:
                row[f] = data[d][f]
            writer.writerow(row)

##########################################################################

def pie_plot(data, label_index, value_index, title, filename, log):
    log.info("Plotting '{t}' to {f}"
             .format(t=title, f=filename))

    labels = list()
    values = list()
    for index, data in data.items():
        labels.append(data[label_index])
        values.append(data[value_index])

    pyplot.pie(values, labels=labels, autopct='%1.1f%%',
               shadow=True)
    pyplot.axis('equal')
    pyplot.title(title)
    pyplot.savefig(filename)
    pyplot.close()

##########################################################################

def main():
    args = setup_cli()

    log = setup_logging(args)

    mailmap = read_mailmap(args.repo, log)

    repo_path = pygit2.discover_repository(args.repo)
    repo = pygit2.Repository(repo_path)

    remote = find_remote(repo, args.remote, log)
    branches = find_branches(repo, remote, None, log)

    now   = datetime.datetime.today()
    td    = datetime.timedelta(days=args.days)
    start = now - td

    # Examine master first, so that it "claims" all the commits in
    # shared branches.
    commits = dict()
    commits = examine_branch(repo, repo.branches[remote.name + '/master'],
                             start, commits,
                             args.skip_merge_commits, log)
    # Now do all the rest of the branches, skipping master.
    for branch in branches:
        if 'master' in branch.name:
            continue
        commits = examine_branch(repo, branch, start, commits,
                                 args.skip_merge_commits, log)

    # commits now holds the SHAs of all commits from all relevant
    # branches on the specified remote for the designated time period.
    committers, domains = examine_contributions(repo, commits, mailmap, log)

    # Write out CSVs
    write_commits_csv(commits, log)
    write_csv(committers, 'committers.csv', log)
    write_csv(domains, 'domains.csv', log)

    # Write out pretty graphs
    pie_plot(committers, 'email', 'num_commits',
             'Number of commits per committer',
             'committers-num-commits-plot.pdf', log)
    pie_plot(committers, 'email', 'num_additions',
             'Number of additions per committer',
             'committers-num-additions-plot.pdf', log)

    pie_plot(domains, 'domain', 'num_commits',
             'Number of commits per domain',
             'domains-num-commits-plot.pdf', log)
    pie_plot(domains, 'domain', 'num_additions',
             'Number of additions per domain',
             'domains-num-additions-plot.pdf', log)

    print("Done!")

if __name__ == '__main__':
    main()
