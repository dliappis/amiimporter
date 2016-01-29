#!/usr/bin/env python

import argparse
import boto
import json
import logging
import os
import re
import subprocess
import sys
import time
from boto.s3.key import Key
from boto import ec2


def make_opt_parser():
    p = argparse.ArgumentParser(description='Import virtualbox vagrant box as AWS AMI')
    p.add_argument('--region', default='eu-central-1')
    p.add_argument('--s3bucket', help='s3bucket', required=True)
    p.add_argument('--s3key',
                   help='s3key e.g. centos-6-hvm-20160125111111'
                   'if ommited your vboxfile must look like e.g. centos6.7-20160101111111')
    p.add_argument('--vboxfile',
                   required=True,
                   help='The vagrant box name.')
    p.add_argument('--tempdir',
                   default='./tmpdir',
                   help="Temporary dir WARNING: "
                   "it will be cleaned up before and after operation",
                   )
    p.add_argument('--verbose',
                   required=False,
                   help='Display status and progress',
                   action='store_true')
    p.add_argument('--debug',
                   required=False,
                   action='store_true')
    return p


def cleanup_temp_dir(p):
    if not os.path.isdir(p.tempdir):
        os.mkdir(p.tempdir)
        return
    [os.remove("{}/{}".format(p.tempdir, file)) for file in os.listdir(p.tempdir)]


def vbox_to_vmdk(p):
    """
    Extract vbox and convert to OVA, required for AWS import
    """
    vmdkfile = None
    try:
        # split basename/dirname; use regexp as basename may contain dot for version
        parsed_vbox_filename = re.search(r'(.+)\.([A-Za-z]+)', p.vboxfile)
        vboxprefix, vboxsuffix = parsed_vbox_filename.group(1), parsed_vbox_filename.group(2)
        subprocess.check_call(["cp", p.vboxfile, p.tempdir])
        subprocess.check_call(["gunzip", "-S", "."+vboxsuffix, os.path.basename(p.vboxfile)], cwd=p.tempdir)
        subprocess.check_call(["tar", "xf", os.path.basename(vboxprefix)], cwd=p.tempdir)

        vmdkfile = [file for file in os.listdir(p.tempdir) if '.vmdk' in file][0]

    except subprocess.CalledProcessError, errVal:
        print("Error while extracting supplied vbox file: {}".format(p.vboxfile))
        print("Reported error is: {}".format(errVal))
        sys.exit(1)
    except IndexError:
        print("Found more tha one .vmdk files after extracting {}".format(p.vboxfile))
        sys.exit(1)

    return "{}/{}".format(p.tempdir, vmdkfile)


def upload_vmdk_to_s3(p, vmdkfile):
    def percent_cb(completed, total):
        if not p.verbose:
            return
        sys.stdout.write("\r%d%%" % (0 if completed == 0 else completed*100/total))
        sys.stdout.flush()

    logging.info("Uploading {} to s3".format(vmdkfile))
    # boto2 doesn't have import-image yet; use aws cli command until we switch to boto3
    if not p.s3key:
        (osname, osver, creationdate) = parse_vbox_name(os.path.basename(p.vboxfile))
        s3file = "temp-hvm-{}-{}-{}".format(osname, osver, creationdate)
        p.s3key = s3file
    else:
        s3file = p.s3key
    AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY_ID = os.environ.get('AWS_SECRET_ACCESS_KEY_ID')

    s3conn = boto.connect_s3(AWS_ACCESS_KEY_ID,
                             AWS_SECRET_ACCESS_KEY_ID)

    bucket = s3conn.get_bucket(p.s3bucket, validate=False)  # see https://github.com/boto/boto/issues/2741
    bucket_location = bucket.get_location()
    if bucket_location:
        conn = boto.s3.connect_to_region(bucket_location)
        p.region = bucket_location
        bucket = conn.get_bucket(p.s3bucket)
    # TODO check if key exists in bucket
    s3key = Key(bucket)
    s3key.key = s3file
    s3key.set_contents_from_filename(vmdkfile, cb=percent_cb, num_cb=10)


def delete_s3key(p):
    AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY_ID = os.environ.get('AWS_SECRET_ACCESS_KEY_ID')

    s3conn = boto.connect_s3(AWS_ACCESS_KEY_ID,
                             AWS_SECRET_ACCESS_KEY_ID)
    bucket = s3conn.get_bucket(p.s3bucket, validate=False)  # see https://github.com/boto/boto/issues/2741
    bucket_location = bucket.get_location()
    if bucket_location:
        conn = boto.s3.connect_to_region(bucket_location)
        p.region = bucket_location
        bucket = conn.get_bucket(p.s3bucket)
    s3key = Key(bucket)
    s3key.key = p.s3key
    logging.info("Deleting updateded s3 file s3://{}/{}".format(p.s3bucket, p.s3key))
    bucket.delete_key(s3key)


def import_s3key_to_ami(p):
    try:
        awsDiskContainer = {'Description': p.s3key,
                            'DiskContainers': [
                                {
                                    'Description': p.s3key,
                                    'UserBucket': {
                                        'S3Bucket': p.s3bucket,
                                        'S3Key': p.s3key
                                    }
                                }
                            ]}
        aws_import_command = [
            'aws',
            '--region', p.region,
            'ec2', 'import-image',
            '--cli-input-json', json.dumps(awsDiskContainer)]
        logging.info("Running: {}".format(' '.join(aws_import_command)))
        importcmd_resp = subprocess.check_output(aws_import_command)
    except subprocess.CalledProcessError:
        logging.error("An error occured while execuring"
                      " ".join(aws_import_command))

    logging.debug(json.loads(importcmd_resp))
    import_task_id = json.loads(importcmd_resp)['ImportTaskId']
    logging.info("AWS is now importing vdmk to AMI.")

    while True:
        aws_import_status_cmd = [
            'aws',
            '--region', p.region,
            'ec2', 'describe-import-image-tasks',
            '--import-task-ids', import_task_id]
        import_progress_resp = json.loads(subprocess.check_output(aws_import_status_cmd))['ImportImageTasks'][0]
        if 'Progress' not in import_progress_resp.keys() and 'ImageId' in import_progress_resp.keys():
            temporary_ami = import_progress_resp['ImageId']
            logging.info("Done, amiid is {}".format(temporary_ami))
            break
        else:
            import_progress = import_progress_resp['Progress']
            sys.stdout.write("\r%s%%" % import_progress)
            sys.stdout.flush()
        time.sleep(5)
    logging.info("Successfully created temporary AMI {}".format(temporary_ami))

    # import-image created randon name and description. Those can't be modified.
    # Create copies for all regions with the right metadata instead.
    amis_created = {}
    for region in ['eu-central-1', 'us-west-2', 'us-east-1']:
        ec2conn = ec2.connect_to_region(region)
        amis_created[region] = ec2conn.copy_image(p.region, temporary_ami, name=p.s3key, description=p.s3key)
        print "Created {} in region {}".format(amis_created[region].image_id, region)

    logging.info("Deregistering temporary AMI {}".format(temporary_ami))
    ec2conn = ec2.connect_to_region(p.region)
    ec2conn.deregister_image(temporary_ami)


def parse_vbox_name(vboxname):
    vbox_tokens = vboxname.split('-')
    osname = re.search('^[a-zA-Z]+', vbox_tokens[0]).group(0)
    osver = re.search('[0-9\.]+', vbox_tokens[0]).group(0)

    if osname == 'ubuntu':
        osver = osver
    elif osname == 'debian':
        osver = osver.split('.')[0]
    elif osname == 'opensuse' or osname == 'sles' or osname == 'oel':
        osver = osver.split('.')[0]
    else:
        osver = osname

    # isodate in UTC
    creationdate = time.strftime("%Y%m%d%H%M%S", time.gmtime())

    return (osname, osver, creationdate)


def main(opts):
    if opts.verbose:
        logging.basicConfig(level=logging.INFO)
    cleanup_temp_dir(opts)
    vmdkfile = vbox_to_vmdk(opts)
    upload_vmdk_to_s3(opts, vmdkfile)
    import_s3key_to_ami(opts)
    delete_s3key(opts)
    cleanup_temp_dir(opts)

if __name__ == '__main__':
    main(make_opt_parser().parse_args(sys.argv[1:]))
