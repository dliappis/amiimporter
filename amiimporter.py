#!/usr/bin/env python
from __future__ import print_function

import argparse
import botocore
import boto3
import json
import logging
import os
import pdb
import re
import subprocess
import sys
import time
import threading


from distutils.version import StrictVersion

__MIN_BOTO3_VER = "1.4.1"


# From http://boto3.readthedocs.io/en/latest/_modules/boto3/s3/transfer.html
class ProgressPercentage(object):
        def __init__(self, filename):
            self._filename = filename
            self._size = float(os.path.getsize(filename))
            self._seen_so_far = 0
            self._lock = threading.Lock()

        def __call__(self, bytes_amount):
            with self._lock:
                self._seen_so_far += bytes_amount
                percentage = (self._seen_so_far / self._size) * 100
                print("{}  {} / {}  {:.2f}%%                                                \r".format(
                    self._filename,
                    self._seen_so_far,
                    self._size,
                    percentage),
                      end='\r')
                sys.stdout.flush()


def check_versions():
    if StrictVersion(boto3.__version__) < StrictVersion(__MIN_BOTO3_VER):
        logging.error("Your boto3 python library is older than {}. Please upgrade.}.format(__MIN_BOTO3_VER)")
        sys.exit(1)


def make_opt_parser():
    p = argparse.ArgumentParser(description='Import virtualbox vagrant box as AWS AMI')
    p.add_argument('--region', default='eu-central-1')
    p.add_argument('--s3bucket', help='s3bucket', required=True)
    p.add_argument('--s3key',
                   help='s3key e.g. centos-6-hvm-20160125111111'
                   'if omitted your vboxfile must look like e.g. centos6.7-20160101111111')
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
        print("Found more than one .vmdk files after extracting {}".format(p.vboxfile))
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

    client = boto3.client('s3', config=botocore.client.Config(signature_version='s3v4'))

    bucket_location = client.get_bucket_location(Bucket=p.s3bucket)
    if bucket_location:
        p.region = bucket_location['LocationConstraint']
    # TODO check if key exists in bucket
    try:
        with open(vmdkfile, 'rb') as data:
            client.upload_fileobj(data, p.s3bucket, s3file, Callback=ProgressPercentage(vmdkfile))
    except botocore.exceptions.ClientError as e:
        print("Error {}".format(e))
        sys.exit(1)


def delete_s3key(p):
    client = boto3.client('s3', config=botocore.client.Config(signature_version='s3v4'))
    bucket_location = client.get_bucket_location(Bucket=p.s3bucket)
    if bucket_location:
        p.region = bucket_location['LocationConstraint']
    logging.info("Deleting uploaded s3 file s3://{}/{}".format(p.s3bucket, p.s3key))
    client.delete_object(
        Bucket=p.s3bucket,
        Key=p.s3key,
    )


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
            logging.info("Done, ami-id is {}".format(temporary_ami))
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
    client = {}
    for dest_region in ['eu-central-1', 'us-west-2', 'us-east-1', 'us-east-2']:
        client[dest_region] = boto3.client('ec2', region_name=dest_region)
        amis_created[dest_region] = client[dest_region].copy_image(
            SourceRegion=p.region,
            SourceImageId=temporary_ami,
            Name=p.s3key,
            Description=p.s3key
        )
        # amis_created[region] = ec2conn.copy_image(p.region, temporary_ami, name=p.s3key, description=p.s3key)
        print("Created {} in region {}".format(amis_created[dest_region]['ImageId'], dest_region))

    logging.info("Deregistering temporary AMI {}".format(temporary_ami))
    client = boto3.client('ec2', region_name=p.region)
    client.deregister_image(ImageId=temporary_ami)
    # ec2conn = ec2.connect_to_region(p.region)
    # ec2conn.deregister_image(temporary_ami)


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
    check_versions()
    cleanup_temp_dir(opts)
    vmdkfile = vbox_to_vmdk(opts)
    upload_vmdk_to_s3(opts, vmdkfile)
    import_s3key_to_ami(opts)
    delete_s3key(opts)
    cleanup_temp_dir(opts)

if __name__ == '__main__':
    main(make_opt_parser().parse_args(sys.argv[1:]))
