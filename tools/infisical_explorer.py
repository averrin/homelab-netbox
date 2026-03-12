import os
import sys
import argparse
import re
from typing import List, Dict, Set, Tuple

import networkx as nx
import matplotlib.pyplot as plt

# Add parent dir to sys.path to be able to import config
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from config import load_config
from infisical_sdk import InfisicalSDKClient

def get_client():
    cfg = load_config().infisical
    if not cfg.is_configured:
        print("Infisical is not configured. Check config.py or env vars.")
        sys.exit(1)

    client = InfisicalSDKClient(host=cfg.url.rstrip("/"))
    client.auth.universal_auth.login(
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
    )
    return client, cfg

def list_folders_recursive(client, cfg, path: str) -> List[str]:
    """Recursively list all folders starting from a given path."""
    all_folders = [path]
    try:
        response = client.folders.list_folders(
            environment_slug=cfg.environment,
            project_id=cfg.project_id,
            path=path
        )
        for folder in response.folders:
            sub_path = os.path.join(path, folder.name).replace("\\", "/")
            if not sub_path.startswith("/"):
                sub_path = "/" + sub_path
            all_folders.extend(list_folders_recursive(client, cfg, sub_path))
    except Exception as e:
        print(f"Failed to list folders at {path}: {e}")
    return all_folders

def get_all_secrets(client, cfg, path: str) -> List[dict]:
    """Get all secrets in a given folder."""
    try:
        response = client.secrets.list_secrets(
            environment_slug=cfg.environment,
            project_id=cfg.project_id,
            secret_path=path,
            expand_secret_references=False,
            include_imports=False
        )
        secrets = []
        for s in response.secrets:
            secrets.append({
                "key": s.secretKey,
                "value": s.secretValue,
                "path": path,
                "full_path": os.path.join(path, s.secretKey).replace("\\", "/")
            })
        return secrets
    except Exception as e:
        print(f"Failed to list secrets at {path}: {e}")
        return []

def collect_data(client, cfg, start_path: str):
    """Collect all folders and secrets from Infisical."""
    print(f"Collecting folders and secrets starting from {start_path}...")
    folders = list_folders_recursive(client, cfg, start_path)
    print(f"Found {len(folders)} folders.")

    all_secrets = []
    for folder in set(folders):
        secrets = get_all_secrets(client, cfg, folder)
        all_secrets.extend(secrets)

    print(f"Found {len(all_secrets)} secrets.")
    return folders, all_secrets

def detect_duplicates(secrets: List[dict]):
    """Find secrets with identical values and print them."""
    value_map = {}
    for s in secrets:
        # Ignore empty values
        if not s['value']:
            continue

        if s['value'] not in value_map:
            value_map[s['value']] = []
        value_map[s['value']].append(f"{s['path']}::{s['key']}")

    duplicates = {v: paths for v, paths in value_map.items() if len(paths) > 1}

    if duplicates:
        print("\n--- Found Duplicate Values ---")
        for val, paths in duplicates.items():
            print(f"Value: '{val}' found in:")
            for p in paths:
                print(f"  - {p}")
            print("  Suggestion: Consider keeping one and referencing it (e.g., ${{{}}})".format(paths[0].split('::')[-1]))
        print("------------------------------\n")
    else:
        print("\nNo duplicates found.\n")


def build_and_draw_graph(folders: List[str], secrets: List[dict], output_file: str):
    """Build a graph of folders, secrets and references, and save to a file."""
    G = nx.DiGraph()

    # Add folders as nodes
    for f in set(folders):
        G.add_node(f, type="folder", color="lightblue")

        # Add folder hierarchy edges
        if f != "/":
            parent = os.path.dirname(f)
            if not parent.startswith("/"):
                parent = "/" + parent
            if parent in folders:
                G.add_edge(parent, f, type="hierarchy")

    # Add secrets and references
    for s in secrets:
        node_name = f"{s['path']}::{s['key']}"
        G.add_node(node_name, type="secret", color="lightgreen", label=s['key'])
        G.add_edge(s['path'], node_name, type="hierarchy")

        # Parse references like ${secretName} or ${pathTo/secretName}
        # Infisical syntax: ${SECRET_NAME} or ${/path/to/secret}
        matches = re.findall(r'\${([^}]+)}', str(s['value']))
        for match in matches:
            if "/" in match:
                ref_path, ref_key = os.path.split(match)
                if not ref_path.startswith("/"):
                     ref_path = "/" + ref_path
                ref_node = f"{ref_path}::{ref_key}"
            else:
                ref_node = f"{s['path']}::{match}"

            G.add_edge(node_name, ref_node, type="reference", color="red")

    print(f"Graph built with {G.number_of_nodes()} nodes and {G.number_of_edges()} edges.")

    # Draw graph
    plt.figure(figsize=(20, 15))
    pos = nx.spring_layout(G, k=0.5, iterations=50)

    colors = [G.nodes[n].get('color', 'gray') for n in G.nodes()]
    labels = {n: G.nodes[n].get('label', n.split("::")[-1] if "::" in n else n) for n in G.nodes()}

    # Draw nodes
    nx.draw_networkx_nodes(G, pos, node_color=colors, node_size=1500, alpha=0.8)

    # Draw edges
    hierarchy_edges = [(u, v) for u, v, d in G.edges(data=True) if d.get('type') == 'hierarchy']
    reference_edges = [(u, v) for u, v, d in G.edges(data=True) if d.get('type') == 'reference']

    nx.draw_networkx_edges(G, pos, edgelist=hierarchy_edges, arrows=True, arrowsize=10, edge_color="gray", style="solid")
    nx.draw_networkx_edges(G, pos, edgelist=reference_edges, arrows=True, arrowsize=15, edge_color="red", style="dashed")

    # Draw labels
    nx.draw_networkx_labels(G, pos, labels, font_size=8, font_weight="bold")

    plt.title("Infisical Secrets Structure and References")
    plt.axis('off')
    plt.tight_layout()
    plt.savefig(output_file, dpi=300)
    print(f"Graph saved to {output_file}")


def update_references(client, cfg, all_secrets: List[dict], old_path: str, old_key: str, new_path: str, new_key: str):
    """Find any secrets referencing the old secret and update their values to point to the new location."""

    def clean_path(p: str) -> str:
        return "/" + p.strip("/") if p.strip("/") else "/"

    old_p = clean_path(old_path)
    new_p = clean_path(new_path)

    # Formats: ${KEY} (same dir) or ${/path/to/KEY} (any dir)
    # We will just normalize to absolute paths for replacement to be safe.
    old_ref_abs = f"${{{old_p}/{old_key}}}"
    if old_p == "/":
        old_ref_abs = f"${{/{old_key}}}"

    new_ref_abs = f"${{{new_p}/{new_key}}}"
    if new_p == "/":
        new_ref_abs = f"${{/{new_key}}}"

    updates_made = 0
    for s in all_secrets:
        val = str(s['value'])
        s_path = clean_path(s['path'])

        # Check for absolute path reference
        if old_ref_abs in val:
            new_val = val.replace(old_ref_abs, new_ref_abs)

            try:
                client.secrets.update_secret_by_name(
                    current_secret_name=s['key'],
                    secret_value=new_val,
                    secret_path=s_path,
                    environment_slug=cfg.environment,
                    project_id=cfg.project_id
                )
                print(f"Updated reference in {s_path}::{s['key']}")
                updates_made += 1
            except Exception as e:
                print(f"Failed to update reference in {s_path}::{s['key']}: {e}")

        # Check for relative path reference (only valid if referencing a secret in the same folder)
        elif old_p == s_path:
            old_ref_rel = f"${{{old_key}}}"
            if old_ref_rel in val:
                # If they are still in the same folder after move, we can use relative.
                # But it's safer to always upgrade to absolute when moving.
                if new_p == s_path:
                     new_ref = f"${{{new_key}}}"
                else:
                     new_ref = new_ref_abs

                new_val = val.replace(old_ref_rel, new_ref)

                try:
                    client.secrets.update_secret_by_name(
                        current_secret_name=s['key'],
                        secret_value=new_val,
                        secret_path=s_path,
                        environment_slug=cfg.environment,
                        project_id=cfg.project_id
                    )
                    print(f"Updated relative reference in {s_path}::{s['key']}")
                    updates_made += 1
                except Exception as e:
                    print(f"Failed to update relative reference in {s_path}::{s['key']}: {e}")

    return updates_made


def move_secret(client, cfg, secret_name: str, source_path: str, dest_path: str):
    """Move a secret to a new path and update references to it."""
    print(f"Moving secret {secret_name} from {source_path} to {dest_path}")

    # Check if secret exists
    secrets = get_all_secrets(client, cfg, source_path)
    secret_obj = next((s for s in secrets if s['key'] == secret_name), None)

    if not secret_obj:
        print(f"Error: Secret {secret_name} not found in {source_path}")
        return

    val = secret_obj['value']

    # Ensure destination path exists
    try:
        # Simplistic approach: attempt to create folder
        client.folders.create_folder(
            name=os.path.basename(dest_path),
            environment_slug=cfg.environment,
            project_id=cfg.project_id,
            path=os.path.dirname(dest_path) if os.path.dirname(dest_path) else "/"
        )
    except Exception:
        pass # May already exist

    # Create secret in new location
    try:
        client.secrets.create_secret_by_name(
            secret_name=secret_name,
            secret_value=val,
            secret_path=dest_path,
            environment_slug=cfg.environment,
            project_id=cfg.project_id
        )
    except Exception as e:
        print(f"Failed to create secret in dest: {e}")
        # Try update if already exists
        try:
             client.secrets.update_secret_by_name(
                current_secret_name=secret_name,
                secret_value=val,
                secret_path=dest_path,
                environment_slug=cfg.environment,
                project_id=cfg.project_id
            )
        except Exception as e2:
             print(f"Failed to update existing secret in dest: {e2}")
             return

    # Collect all secrets globally to check for references
    _, all_secrets = collect_data(client, cfg, "/")

    # Update references
    update_references(client, cfg, all_secrets, source_path, secret_name, dest_path, secret_name)

    # Delete original secret
    try:
        client.secrets.delete_secret_by_name(
            secret_name=secret_name,
            secret_path=source_path,
            environment_slug=cfg.environment,
            project_id=cfg.project_id
        )
        print(f"Deleted old secret {secret_name} from {source_path}")
    except Exception as e:
        print(f"Failed to delete old secret: {e}")


def move_folder(client, cfg, source_path: str, dest_path: str):
    """Move a folder to a new path. Recursively moves all secrets and subfolders, updating references."""
    print(f"Moving folder {source_path} to {dest_path}")

    def clean_path(p: str) -> str:
        return "/" + p.strip("/") if p.strip("/") else "/"

    source_p = clean_path(source_path)
    dest_p = clean_path(dest_path)

    if source_p == "/" or source_p == "":
        print("Cannot move root folder.")
        return

    # Ensure destination folder exists
    try:
        client.folders.create_folder(
            name=os.path.basename(dest_p),
            environment_slug=cfg.environment,
            project_id=cfg.project_id,
            path=os.path.dirname(dest_p) if os.path.dirname(dest_p) else "/"
        )
    except Exception:
        pass

    # Get all subfolders and secrets within source_path
    subfolders = list_folders_recursive(client, cfg, source_p)

    for folder in sorted(subfolders):
        # Calculate new folder path
        rel_path = folder[len(source_p):]
        new_folder_path = clean_path(dest_p + rel_path)

        # Ensure new subfolder exists
        if rel_path:
             try:
                 client.folders.create_folder(
                     name=os.path.basename(new_folder_path),
                     environment_slug=cfg.environment,
                     project_id=cfg.project_id,
                     path=os.path.dirname(new_folder_path) if os.path.dirname(new_folder_path) else "/"
                 )
             except Exception:
                 pass

        # Move secrets in this folder
        secrets = get_all_secrets(client, cfg, folder)
        for s in secrets:
            move_secret(client, cfg, s['key'], folder, new_folder_path)

    # Finally delete the old folders (bottom up)
    for folder in sorted(subfolders, reverse=True):
        try:
            client.folders.delete_folder(
                name=os.path.basename(folder),
                environment_slug=cfg.environment,
                project_id=cfg.project_id,
                path=os.path.dirname(folder) if os.path.dirname(folder) else "/"
            )
            print(f"Deleted old folder {folder}")
        except Exception as e:
            print(f"Failed to delete folder {folder}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Infisical Explorer Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # explore command
    explore_parser = subparsers.add_parser("explore", help="Explore secrets and build a visual graph")
    explore_parser.add_argument("--path", type=str, default="/", help="Starting path")
    explore_parser.add_argument("--output", type=str, default="infisical_graph.png", help="Output image file")

    # move-secret command
    move_secret_parser = subparsers.add_parser("move-secret", help="Move a secret and update references")
    move_secret_parser.add_argument("--secret-name", required=True, help="Name of the secret to move")
    move_secret_parser.add_argument("--source-path", required=True, help="Current path of the secret")
    move_secret_parser.add_argument("--dest-path", required=True, help="New path for the secret")

    # move-folder command
    move_folder_parser = subparsers.add_parser("move-folder", help="Move a folder and update references")
    move_folder_parser.add_argument("--source-path", required=True, help="Current path of the folder")
    move_folder_parser.add_argument("--dest-path", required=True, help="New path for the folder")

    args = parser.parse_args()

    client, cfg = get_client()

    if args.command == "explore":
        print(f"Exploring path: {args.path}")
        folders, secrets = collect_data(client, cfg, args.path)
        detect_duplicates(secrets)
        build_and_draw_graph(folders, secrets, args.output)
    elif args.command == "move-secret":
        move_secret(client, cfg, args.secret_name, args.source_path, args.dest_path)
    elif args.command == "move-folder":
        move_folder(client, cfg, args.source_path, args.dest_path)

if __name__ == "__main__":
    main()
