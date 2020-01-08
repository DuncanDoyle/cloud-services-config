import copy
import json
import re
import sys
import time
import update_api_utilties as util


# Creates a new version of the property in Akamai,
# which is based off of the latest active version in the given environment.
def createNewVersion(property_env="STAGING"):
    # Get the number of the latest prod version to use as a base
    previous_version = util.getLatestVersionNumber(property_env)

    # Save this number for later: create a file that contains the latest version number
    with open("previousversion.txt", "w") as f:
        f.write(str(previous_version))

    body = {
        "createFromVersion": previous_version
    }
    
    print("API - Creating new version based on v{}".format(previous_version))
    response_content = json.loads(util.akamaiPost("/papi/v1/properties/prp_516561/versions?contractId=ctr_3-1MMN3Z&groupId=grp_134508",body))

    new_version = 0
    m = re.search('versions\/(.+?)\?contractId', response_content["versionLink"])
    if m:
        new_version = m.group(1)
    print("Version {} created.".format(new_version))

    # Save this number for later: create a file that contains the new version number
    with open("newversion.txt", "w") as f:
        f.write(str(new_version))
    return new_version

# Creates a list of rules in the correct Akamai PM structure based on
# the master_config (source of truth), and prepends paths with
# global_path_prefix as appropriate.
def createRulesForEnv(master_config, global_path_prefix=""):
    # First, add the rules for the landing page.
    rules = util.getJSONFromFile("./data/landing_page_rules.json")
    rules.extend(util.getJSONFromFile("./data/storybook_rules.json"))

    # If global path prefix exists, modify paths on landing page rules.
    if global_path_prefix != "":
        for rule in rules:
            if rule["behaviors"][0]["name"] == "failAction":
                rule["behaviors"][0]["options"]["contentPath"] = global_path_prefix + rule["behaviors"][0]["options"]["contentPath"]
            for x in range(len(rule["criteria"])):
                if rule["criteria"][x]["name"] == "path":
                    for y in range(len(rule["criteria"][x]["options"]["values"])):
                        if rule["criteria"][x]["options"]["values"][y] == "/":
                            rule["criteria"][x]["options"]["values"].append(global_path_prefix)
                        rule["criteria"][x]["options"]["values"][y] = global_path_prefix + rule["criteria"][x]["options"]["values"][y]

    # Create a template object to copy from (reduces number of read/write ops)
    rule_template = util.getJSONFromFile("./data/single_rule_template.json")
    nomatch_template = util.getJSONFromFile("./data/no_match_criteria.json")

    # Creates rules for all the apps that follow a pattern.
    for key, app in master_config.items():
        if "frontend" in app and "paths" in app["frontend"] and not ("disabled_on_prod" in app and app["disabled_on_prod"]):
            app_rule = copy.deepcopy(rule_template)
            app_rule["name"] = "/" + key
            app_path = app["frontend"]["app_base"] if "app_base" in app["frontend"] else key
            app_rule["behaviors"][0]["options"]["contentPath"] = "{}/apps/{}/index.html".format(global_path_prefix, app_path)
            for frontend_path in app["frontend"]["paths"]:
                values = [global_path_prefix + frontend_path]
                values += [global_path_prefix + frontend_path + "/*"]
                app_rule["criteria"][0]["options"]["values"].extend(values)

            if "frontend_exclude" in app and len(app["frontend_exclude"]) > 0:
                app_criteria = copy.deepcopy(nomatch_template)
                for nomatch in app["frontend_exclude"]:
                    app_criteria["options"]["values"].append(global_path_prefix + nomatch)
                    app_criteria["options"]["values"].append(global_path_prefix + nomatch + "/*")
                app_rule["criteria"].append(app_criteria)

            rules.append(app_rule)

    return rules

# Makes an API call which updates the property version with a new rule tree.
def updatePropertyRulesUsingConfig(version_number, master_config_list):
    print("Creating new ruleset based on list of master configs...")
    rules_tree = util.getJSONFromFile("./data/base_rules.json")

    parent_rule_template = util.getJSONFromFile("./data/base_env_rule.json")
    
    # Iterate through the configurations for each release
    for env in master_config_list:
        parent_rule = copy.deepcopy(parent_rule_template)
        parent_rule["name"] = "{} (AUTO-GENERATED)".format(env["name"])
        parent_rule["criteria"][0]["options"]["matchOperator"] = "DOES_NOT_MATCH_ONE_OF" if ("prefix" not in env or env["prefix"] == "") else "MATCHES_ONE_OF"
        if ("prefix" not in env or env["prefix"] == ""):
            parent_rule["criteria"][0]["options"]["values"].append("/api")
            parent_rule["criteria"][0]["options"]["values"].append("/api/*")
            # Each env should exclude matches for other envs.
            for nomatch in (x for x in master_config_list if (x != env["name"] and "prefix" in x and x["prefix"] != "")):
                parent_rule["criteria"][0]["options"]["values"].append(nomatch["prefix"])
                parent_rule["criteria"][0]["options"]["values"].append(nomatch["prefix"] + "/*")
        else:
            parent_rule["criteria"][0]["options"]["values"].append(env["prefix"])
            parent_rule["criteria"][0]["options"]["values"].append(env["prefix"] + "/*")
            
        parent_rule["children"] = createRulesForEnv(env["config"], env["prefix"])
        rules_tree["rules"]["children"][2]["children"].append(parent_rule)

    # Update property with this new ruleset
    print("API - Updating rule tree...")
    response = json.loads(util.akamaiPut("/papi/v1/properties/prp_516561/versions/{}/rules?contractId=ctr_3-1MMN3Z&groupId=grp_134508&validateRules=true&validateMode=full".format(version_number),rules_tree))

def generateExclusions(frontend_path, config):
    exclusions = []
    for key in (x for x in config.keys() if "frontend" in config[x] and "paths" in config[x]["frontend"] and frontend_path not in config[x]["frontend"]["paths"]):
        for path in config[key]["frontend"]["paths"]:
            if frontend_path in path:
                exclusions.append(path)
    return exclusions

def generateConfigForBranch(prefix):
    config = util.getYMLFromUrl("https://cloud.redhat.com{}/config/main.yml".format(prefix))
    # For every app in config, check all other apps to see if they have a frontend_path that contains its frontend_paths.
    for key in (x for x in config.keys() if "frontend" in config[x] and "paths" in config[x]["frontend"]):
        exclusions = []
        for fe_path in config[key]["frontend"]["paths"]:
            exclusions.extend(generateExclusions(fe_path, config))
        config[key]["frontend_exclude"] = exclusions
    
    return config

def waitForActiveVersion(version_number, env="STAGING"):
    print("Waiting for version {} to finish activating...".format(version_number))
    active_version = ""
    timeout = 180
    while active_version != version_number:
        time.sleep(10)
        try:
            active_version = util.getLatestVersionNumber(env)
        except:
            print("Failed to retrieve current version")
        timeout -= 1
        if(timeout == 0):
            sys.exit("Retried too many times! New version not activated.")
        print("Property active in {} is v{}".format(env, active_version))
    print("Success! Property v{} now active on {}.".format(active_version, env))
    
def main():
    # Authenticate with EdgeGrid
    # TODO: Change this authentication to get rid of the httpie dependency. Apprently there's a vulnerability
    util.initEdgeGridAuth()

    # Get the Cloud Services config files (main source of truth) for all configured releases
    releases = util.getYMLFromFile("../releases.yml")
    cs_config_list = []
    for env in releases:
        cs_config_list.append({
            "name": env,
            "branch": releases[env]["branch"],
            "prefix": releases[env]["prefix"] if "prefix" in releases[env] else "",
            "config": generateConfigForBranch(releases[env]["prefix"] if "prefix" in releases[env] else "")
        })

    if len(sys.argv) > 2:
        property_env = sys.argv[2]
    else:
        property_env = "STAGING"

    # Create a new version based off of the active Prod version
    new_version_number = createNewVersion(property_env)

    # Update the rules JSON using the CS configuration as a reference
    updatePropertyRulesUsingConfig(new_version_number, cs_config_list)

    # Activate version
    util.activateVersion(new_version_number, property_env)

    # Wait for new version to be active
    waitForActiveVersion(int(new_version_number), property_env)

if __name__== "__main__":
    main()
