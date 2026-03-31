#!/usr/bin/env python3
"""RQ2 Commit Selection: Discover commits, analyze changed functions, select commits"""

import os
import sys
import json
import argparse
import subprocess
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
from pathlib import Path

scripts_dir = Path(__file__).parent.parent.parent
sys.path.insert(0, str(scripts_dir))

try:
    import requests
    import boto3
    from botocore.exceptions import ClientError
    import git
    from tree_sitter import Parser
except ImportError as e:
    print(f"Error: Missing dependency: {e}", file=sys.stderr)
    sys.exit(1)

from scheduling.detect_cpp_changes import detect_cpp_changes
from scripts.diff_utils import FileDiff, parse_unified_diff


class EvaluationS3Manager:
    def __init__(self, bucket: str, solver: str, region: Optional[str] = None):
        self.bucket = bucket
        self.base_path = f"evaluation/rq2/{solver}"
        self.s3_client = boto3.client('s3', region_name=region or os.getenv('AWS_REGION', 'eu-north-1'))
    
    def write_json(self, filename: str, data: any) -> None:
        s3_key = f"{self.base_path}/{filename}"
        self.s3_client.put_object(
            Bucket=self.bucket,
            Key=s3_key,
            Body=json.dumps(data, indent=2).encode('utf-8'),
            ContentType='application/json'
        )
        print(f"✅ Wrote {filename} to S3")
    
    def read_json(self, filename: str, default: Optional[any] = None) -> any:
        try:
            response = self.s3_client.get_object(Bucket=self.bucket, Key=f"{self.base_path}/{filename}")
            return json.loads(response['Body'].read().decode('utf-8'))
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') == 'NoSuchKey':
                return default
            raise


def get_commits_from_last_n_years(repo_url: str, years: int = 2, token: Optional[str] = None) -> List[Dict]:
    repo_path = repo_url.replace('https://github.com/', '').replace('.git', '')
    api_url = f"https://api.github.com/repos/{repo_path}/commits"
    headers = {'Authorization': f'token {token}'} if token else {}
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=years * 365)
    
    commits = []
    page = 1
    params = {'per_page': 100, 'since': cutoff_date.isoformat(), 'page': page}
    
    while True:
        response = requests.get(api_url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        if not data:
            break
        
        for commit in data:
            commit_date = datetime.fromisoformat(commit['commit']['author']['date'].replace('Z', '+00:00'))
            if commit_date < cutoff_date and page > 1:
                return commits
            commits.append({
                'hash': commit['sha'],
                'date': commit_date.isoformat(),
                'message': commit['commit']['message'].split('\n')[0]
            })
        
        if len(data) < 100:
            break
        page += 1
        params['page'] = page
    
    return commits


def filter_cpp_commits(commits: List[Dict], repo_url: str, token: Optional[str] = None) -> List[Dict]:
    cpp_commits = []
    for commit in commits:
        try:
            has_cpp, changed_files = detect_cpp_changes(repo_url, commit['hash'], token)
            if has_cpp:
                commit['changed_files'] = changed_files
                cpp_commits.append(commit)
                print(f"✅ {commit['hash'][:8]} has C++ changes")
        except Exception as e:
            print(f"⚠️  {commit['hash'][:8]}: {e}")
    return cpp_commits


def init_tree_sitter():
    try:
        from tree_sitter_languages import get_language, get_parser
        return get_language('cpp'), get_parser('cpp')
    except ImportError:
        try:
            import tree_sitter_cpp as cpp
            parser = Parser()
            parser.set_language(cpp.language())
            return cpp.language(), parser
        except ImportError:
            raise RuntimeError("Install: pip install tree-sitter-languages")


def parse_diff(diff_text: str) -> Dict[str, FileDiff]:
    return parse_unified_diff(diff_text)


FUNCTION_QUERY = """
(
  [
    (function_definition)
    (declaration)
    (field_declaration)
  ] @func.node
  .
  (function_declarator
    declarator: [
      (identifier)
      (field_identifier)
      (qualified_identifier)
      (operator_name)
      (destructor_name)
    ] @func.name)
)
"""


def analyze_commit_functions(commit_hash: str, repo_path: str, solver: str) -> Dict:
    repo = git.Repo(repo_path)
    try:
        commit = repo.commit(commit_hash)
        parent_hash = commit.parents[0].hexsha if commit.parents else None
    except Exception:
        parent_hash = None
    
    diff_result = subprocess.run(['git', 'show', '-U0', '--no-color', commit_hash],
                                 capture_output=True, text=True, cwd=repo_path)
    if diff_result.returncode != 0:
        raise RuntimeError(f"Failed to get diff for {commit_hash}")
    
    changed_files = parse_diff(diff_result.stdout)
    cpp_language, parser = init_tree_sitter()
    query = cpp_language.query(FUNCTION_QUERY)
    
    function_details = []
    files_with_no_functions = []
    cpp_exts = {'.cpp', '.cc', '.cxx', '.c', '.h', '.hpp', '.hxx', '.hh'}

    def read_file_at_commit(revision: Optional[str], file_path: Optional[str]) -> Optional[str]:
        if not revision or not file_path or file_path == '/dev/null':
            return None
        result = subprocess.run(['git', 'show', f'{revision}:{file_path}'],
                                capture_output=True, text=True, cwd=repo_path)
        if result.returncode != 0:
            return None
        return result.stdout

    def normalize_code(code: str) -> str:
        code = re.sub(r'//.*', '', code)
        code = re.sub(r'/\*.*?\*/', '', code, flags=re.S)
        code = re.sub(r'\s+', ' ', code).strip()
        return code

    def extract_functions(source_text: Optional[str], file_path: str) -> List[Dict]:
        if source_text is None:
            return []

        file_bytes = source_text.encode('utf8')
        try:
            tree = parser.parse(file_bytes)
        except Exception:
            return []

        captures = query.captures(tree.root_node)
        func_map = {}

        for node, tag in captures:
            if tag == "func.name":
                func_node = node.parent
                while func_node and func_node.type not in ("function_definition", "declaration", "field_declaration"):
                    func_node = func_node.parent
                if func_node:
                    func_map[func_node] = file_bytes[node.start_byte:node.end_byte].decode('utf8', errors='ignore')

        functions = []
        lines = source_text.splitlines()
        for func_node, func_name in func_map.items():
            start_line = func_node.start_point[0] + 1
            end_line = func_node.end_point[0] + 1
            body = "\n".join(lines[start_line - 1:end_line])
            functions.append({
                'file': file_path,
                'function': func_name,
                'line': start_line,
                'end_line': end_line,
                'body': normalize_code(body),
            })
        return functions

    def function_key(function: Dict) -> tuple[str, str]:
        return function['file'], function['function']

    for file_path, file_diff in changed_files.items():
        if not any(file_path.endswith(ext) for ext in cpp_exts):
            continue

        after_path = file_diff.new_path if file_diff.new_path != '/dev/null' else None
        before_path = file_diff.old_path if file_diff.old_path != '/dev/null' else None
        after_functions = extract_functions(read_file_at_commit(commit_hash, after_path), after_path or file_path)
        before_functions = extract_functions(read_file_at_commit(parent_hash, before_path), before_path or file_path)

        after_by_key = {function_key(func): func for func in after_functions}
        before_by_key = {function_key(func): func for func in before_functions}

        selected: Dict[tuple[str, str], Dict] = {}
        for func in after_functions:
            if file_diff.overlaps_after(func['line'], func['end_line']):
                selected[function_key(func)] = func

        for func in before_functions:
            key = function_key(func)
            if key not in selected and file_diff.overlaps_before(func['line'], func['end_line']):
                selected[key] = func

        found_functions = set()
        for key, func in selected.items():
            after_func = after_by_key.get(key)
            before_func = before_by_key.get(key)
            if after_func and before_func and after_func['body'] == before_func['body']:
                continue

            func_key = (func['file'], func['function'], func['line'])
            if func_key in found_functions:
                continue
            found_functions.add(func_key)
            function_details.append({'file': func['file'], 'function': func['function'], 'line': func['line']})

        if not found_functions:
            files_with_no_functions.append(file_path)
    
    return {
        'changed_functions_count': len(function_details),
        'changed_functions': function_details,
        'files_with_no_functions': files_with_no_functions
    }


def categorize_commits(commits: List[Dict], small_threshold: int = 5, medium_threshold: int = 20) -> Dict:
    small, medium, large = [], [], []
    for commit in commits:
        count = commit.get('changed_functions_count', 0)
        (small if count <= small_threshold else medium if count <= medium_threshold else large).append(commit)
    
    return {
        'small': small, 'medium': medium, 'large': large,
        'statistics': {'total': len(commits), 'small': len(small), 'medium': len(medium), 'large': len(large)}
    }


def select_commits(categorized: Dict, small_count: int, medium_count: int, large_count: int) -> List[str]:
    return ([c['hash'] for c in categorized['small'][:small_count]] +
            [c['hash'] for c in categorized['medium'][:medium_count]] +
            [c['hash'] for c in categorized['large'][:large_count]])


def main():
    parser = argparse.ArgumentParser(description='RQ2 Commit Selection')
    parser.add_argument('solver', choices=['z3', 'cvc5', 'opensmt'])
    parser.add_argument('repo_url')
    parser.add_argument('--years', type=int, default=2)
    parser.add_argument('--token', default=os.getenv('GITHUB_TOKEN'))
    parser.add_argument('--repo-path', default='.')
    parser.add_argument('--small-count', type=int, default=17)
    parser.add_argument('--medium-count', type=int, default=17)
    parser.add_argument('--large-count', type=int, default=16)
    parser.add_argument('--small-threshold', type=int, default=5)
    parser.add_argument('--medium-threshold', type=int, default=20)
    parser.add_argument('--max-commits', type=int, help='Maximum total commits to select (for testing, limits selection)')
    parser.add_argument('--skip-analysis', action='store_true')
    parser.add_argument('--skip-selection', action='store_true')
    args = parser.parse_args()
    
    bucket = os.getenv('AWS_S3_BUCKET')
    if not bucket:
        raise RuntimeError("AWS_S3_BUCKET environment variable not set")
    
    s3 = EvaluationS3Manager(bucket, args.solver)
    
    print(f"🔍 Discovering commits from last {args.years} years...")
    all_commits = get_commits_from_last_n_years(args.repo_url, args.years, args.token)
    print(f"✅ Found {len(all_commits)} commits")
    
    print(f"🔍 Filtering commits with C++ changes...")
    cpp_commits = filter_cpp_commits(all_commits, args.repo_url, args.token)
    print(f"✅ Found {len(cpp_commits)} commits with C++ changes")
    s3.write_json('raw-commits.json', cpp_commits)
    
    if not args.skip_analysis:
        print(f"🔍 Analyzing changed functions...")
        existing_stats = s3.read_json('commit-statistics.json')
        analyzed_commits = existing_stats.get('commits', []) if existing_stats else []
        analyzed_hashes = {c['hash'] for c in analyzed_commits}
        
        for commit in cpp_commits:
            if commit['hash'] in analyzed_hashes:
                print(f"⏭️  Skipping {commit['hash'][:8]} (already analyzed)")
                continue
            
            print(f"🔍 Analyzing {commit['hash'][:8]}...")
            commit.update(analyze_commit_functions(commit['hash'], args.repo_path, args.solver))
            analyzed_commits.append(commit)
            analyzed_hashes.add(commit['hash'])
        
        categorized = categorize_commits(analyzed_commits, args.small_threshold, args.medium_threshold)
        s3.write_json('commit-statistics.json', {
            'commits': analyzed_commits,
            'statistics': categorized['statistics']
        })
        print(f"✅ Statistics: {categorized['statistics']}")
    else:
        stats = s3.read_json('commit-statistics.json')
        if not stats:
            raise RuntimeError("No existing statistics found")
        analyzed_commits = stats['commits']
        categorized = categorize_commits(analyzed_commits, args.small_threshold, args.medium_threshold)
    
    if not args.skip_selection:
        selected = select_commits(categorized, args.small_count, args.medium_count, args.large_count)
        
        # Limit total commits if specified
        if args.max_commits and args.max_commits > 0:
            original_count = len(selected)
            selected = selected[:args.max_commits]
            print(f"📝 Limited selection from {original_count} to {len(selected)} commits (max_commits={args.max_commits})")
        
        print(f"✅ Selected {len(selected)} commits")
        s3.write_json('selected-commits.json', selected)
    
    print("✅ Commit selection complete!")


if __name__ == '__main__':
    main()
